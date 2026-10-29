"""Official Spotify Web API source (optional fallback to SMTC).

* Authorization Code flow with PKCE - no client secret is needed or stored.
* Redirect URI is a loopback address (http://127.0.0.1:<port>/callback), served by a one-shot
  local listener that only accepts a single callback request.
* Tokens are encrypted with Windows DPAPI (current user) before being written to disk.
* Only polled while SMTC has no Spotify session (e.g. Spotify playing on a phone or another PC).
* Playback control via the Web API requires Spotify Premium; a 403 is reported, never bypassed.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from app.media.base import MediaSource
from app.media.cover_cache import MAX_COVER_BYTES, CoverCache
from app.media.models import Capabilities, Command, MediaState, PlaybackStatus, RepeatMode

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
SCOPES = "user-read-playback-state user-read-currently-playing user-modify-playback-state"
HTTP_TIMEOUT = 8
AUTH_TIMEOUT_S = 180

POLL_PLAYING_MS = 3000
POLL_IDLE_MS = 8000
MAX_BACKOFF_MS = 60000


def redirect_uri_for(port: int) -> str:
    return f"http://127.0.0.1:{port}/callback"


# --------------------------------------------------------------------------- errors
class ApiErrorKind(Enum):
    NOT_CONFIGURED = "not_configured"
    AUTH_REQUIRED = "auth_required"
    PREMIUM_REQUIRED = "premium_required"
    NO_ACTIVE_DEVICE = "no_active_device"
    RATE_LIMITED = "rate_limited"
    NETWORK = "network"
    SERVER = "server"
    BAD_RESPONSE = "bad_response"


class SpotifyApiError(Exception):
    def __init__(self, kind: ApiErrorKind, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after


# --------------------------------------------------------------------------- tokens
@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""

    def expired(self, now: float, margin: float = 60.0) -> bool:
        return now >= self.expires_at - margin


class TokenStore:
    """Token persistence encrypted with DPAPI (readable only by the current Windows user)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> Token | None:
        if not self.path.exists():
            return None
        try:
            import win32crypt

            _, raw = win32crypt.CryptUnprotectData(self.path.read_bytes(), None, None, None, 0)
            data = json.loads(raw.decode("utf-8"))
            return Token(data["access_token"], data["refresh_token"], float(data["expires_at"]), data.get("scope", ""))
        except Exception as exc:
            log.warning("Stored Spotify token is unreadable (%s); re-authorization required", type(exc).__name__)
            return None

    def save(self, token: Token) -> None:
        import win32crypt

        raw = json.dumps(token.__dict__).encode("utf-8")
        blob = win32crypt.CryptProtectData(raw, "DynamicIsland Spotify token", None, None, None, 0)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        tmp.replace(self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Cannot delete token file: %s", exc)


# --------------------------------------------------------------------------- PKCE / auth
def make_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "scope": SCOPES,
        "state": state,
    })
    return f"{AUTH_URL}?{query}"


_CALLBACK_PAGE = """<!doctype html><meta charset="utf-8"><title>Dynamic Island</title>
<body style="font-family:Segoe UI,sans-serif;background:#111;color:#eee;display:flex;
align-items:center;justify-content:center;height:90vh"><div><h2>{title}</h2><p>{text}</p></div></body>"""


def wait_for_auth_code(port: int, expected_state: str, cancel: threading.Event,
                       timeout_s: float = AUTH_TIMEOUT_S) -> str:
    """Listen on 127.0.0.1:<port> for the OAuth redirect and return the authorization code."""
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server naming
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/callback":
                self.send_error(404)
                return
            if params.get("state", [""])[0] != expected_state:
                result["error"] = "state_mismatch"
                title, text = "Ошибка авторизации", "Неверный параметр state. Повторите подключение."
            elif "error" in params:
                result["error"] = params["error"][0]
                title, text = "Доступ не предоставлен", html.escape(params["error"][0])
            elif "code" in params:
                result["code"] = params["code"][0]
                title, text = "Spotify подключён", "Эту вкладку можно закрыть."
            else:
                result["error"] = "no_code"
                title, text = "Ошибка авторизации", "Spotify не вернул код."
            body = _CALLBACK_PAGE.format(title=title, text=text).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the query string (auth code) out of logs
            pass

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise SpotifyApiError(ApiErrorKind.NETWORK, f"Порт {port} занят: {exc.strerror or exc}") from exc
    server.timeout = 0.5
    deadline = time.monotonic() + timeout_s
    try:
        while not result and not cancel.is_set() and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "code" in result:
        return result["code"]
    if cancel.is_set():
        raise SpotifyApiError(ApiErrorKind.AUTH_REQUIRED, "Подключение отменено")
    if "error" in result:
        raise SpotifyApiError(ApiErrorKind.AUTH_REQUIRED, f"Авторизация отклонена: {result['error']}")
    raise SpotifyApiError(ApiErrorKind.AUTH_REQUIRED, "Время ожидания авторизации истекло")


# --------------------------------------------------------------------------- client
class SpotifyApiClient:
    def __init__(self, client_id: str, redirect_uri: str, store: TokenStore,
                 session: requests.Session | None = None, clock=time.time) -> None:
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.store = store
        self.http = session or requests.Session()
        self.clock = clock
        self.token: Token | None = store.load()

    @property
    def authorized(self) -> bool:
        return self.token is not None

    # ------------------------------------------------------------- token endpoint
    def _token_request(self, data: dict) -> dict:
        try:
            resp = self.http.post(TOKEN_URL, data=data, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            raise SpotifyApiError(ApiErrorKind.NETWORK, "Нет соединения со Spotify") from exc
        if resp.status_code == 429:
            raise SpotifyApiError(ApiErrorKind.RATE_LIMITED, "Слишком много запросов", _retry_after(resp))
        if resp.status_code in (400, 401):
            raise SpotifyApiError(ApiErrorKind.AUTH_REQUIRED, "Требуется повторная авторизация Spotify")
        if resp.status_code >= 500:
            raise SpotifyApiError(ApiErrorKind.SERVER, f"Сервер Spotify недоступен ({resp.status_code})")
        try:
            payload = resp.json()
            _ = payload["access_token"]
        except (ValueError, KeyError) as exc:
            raise SpotifyApiError(ApiErrorKind.BAD_RESPONSE, "Некорректный ответ сервера авторизации") from exc
        return payload

    def _store_token(self, payload: dict, previous_refresh: str = "") -> None:
        self.token = Token(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token") or previous_refresh,
            expires_at=self.clock() + float(payload.get("expires_in", 3600)),
            scope=payload.get("scope", ""),
        )
        try:
            self.store.save(self.token)
        except Exception as exc:
            log.warning("Cannot persist Spotify token: %s", type(exc).__name__)

    def exchange_code(self, code: str, verifier: str) -> None:
        payload = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": verifier,
        })
        self._store_token(payload)
        log.info("Spotify authorization completed")

    def refresh(self) -> None:
        if self.token is None or not self.token.refresh_token:
            raise SpotifyApiError(ApiErrorKind.AUTH_REQUIRED, "Spotify не подключён")
        try:
            payload = self._token_request({
                "grant_type": "refresh_token",
                "refresh_token": self.token.refresh_token,
                "client_id": self.client_id,
            })
        except SpotifyApiError as exc:
            if exc.kind is ApiErrorKind.AUTH_REQUIRED:
                self.logout()
            raise
        self._store_token(payload, previous_refresh=self.token.refresh_token)
        log.info("Spotify access token refreshed")

    def logout(self) -> None:
        self.token = None
        self.store.clear()

    # ------------------------------------------------------------- API calls
    def _request(self, method: str, path: str, *, _retry: bool = True, **kwargs) -> requests.Response:
        if self.token is None:
            raise SpotifyApiError(ApiErrorKind.AUTH_REQUIRED, "Spotify не подключён")
        if self.token.expired(self.clock()):
            self.refresh()
        headers = {"Authorization": f"Bearer {self.token.access_token}"}
        try:
            resp = self.http.request(method, API_BASE + path, headers=headers, timeout=HTTP_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            raise SpotifyApiError(ApiErrorKind.NETWORK, "Нет соединения со Spotify") from exc

        if resp.status_code == 401 and _retry:
            self.refresh()
            return self._request(method, path, _retry=False, **kwargs)
        if resp.status_code == 401:
            raise SpotifyApiError(ApiErrorKind.AUTH_REQUIRED, "Требуется повторная авторизация Spotify")
        if resp.status_code == 429:
            raise SpotifyApiError(ApiErrorKind.RATE_LIMITED, "Превышен лимит запросов Spotify", _retry_after(resp))
        if resp.status_code == 403:
            reason = _error_reason(resp)
            if reason == "PREMIUM_REQUIRED" or "premium" in reason.lower():
                raise SpotifyApiError(ApiErrorKind.PREMIUM_REQUIRED, "Управление через Web API требует Spotify Premium")
            raise SpotifyApiError(ApiErrorKind.AUTH_REQUIRED, "Spotify отказал в доступе (403)")
        if resp.status_code == 404 and path.startswith("/me/player"):
            raise SpotifyApiError(ApiErrorKind.NO_ACTIVE_DEVICE, "Нет активного устройства Spotify")
        if resp.status_code >= 500:
            raise SpotifyApiError(ApiErrorKind.SERVER, f"Сервер Spotify недоступен ({resp.status_code})")
        if resp.status_code >= 400:
            raise SpotifyApiError(ApiErrorKind.BAD_RESPONSE, f"Ошибка Spotify API ({resp.status_code})")
        return resp

    def get_profile(self) -> dict:
        resp = self._request("GET", "/me")
        try:
            return resp.json()
        except ValueError as exc:
            raise SpotifyApiError(ApiErrorKind.BAD_RESPONSE, "Некорректный ответ Spotify") from exc

    def get_playback(self) -> dict | None:
        resp = self._request("GET", "/me/player", params={"additional_types": "episode"})
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise SpotifyApiError(ApiErrorKind.BAD_RESPONSE, "Некорректный ответ Spotify") from exc

    def command(self, command: Command, currently_playing: bool) -> None:
        if command is Command.TOGGLE:
            command = Command.PAUSE if currently_playing else Command.PLAY
        method, path = {
            Command.NEXT: ("POST", "/me/player/next"),
            Command.PREVIOUS: ("POST", "/me/player/previous"),
            Command.PLAY: ("PUT", "/me/player/play"),
            Command.PAUSE: ("PUT", "/me/player/pause"),
        }[command]
        self._request(method, path)

    def seek(self, position_ms: int) -> None:
        self._request("PUT", "/me/player/seek", params={"position_ms": max(0, int(position_ms))})

    def set_volume(self, percent: int) -> None:
        self._request("PUT", "/me/player/volume", params={"volume_percent": min(100, max(0, int(percent)))})

    def set_repeat(self, state: str) -> None:
        self._request("PUT", "/me/player/repeat", params={"state": state})

    def download(self, url: str) -> bytes:
        if not url.startswith("https://"):
            raise SpotifyApiError(ApiErrorKind.BAD_RESPONSE, "Небезопасный адрес обложки")
        try:
            resp = self.http.get(url, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.content
        except requests.RequestException as exc:
            raise SpotifyApiError(ApiErrorKind.NETWORK, "Не удалось загрузить обложку") from exc
        if len(data) > MAX_COVER_BYTES:
            raise SpotifyApiError(ApiErrorKind.BAD_RESPONSE, "Обложка слишком большая")
        return data


def _retry_after(resp: requests.Response) -> float:
    try:
        return max(1.0, float(resp.headers.get("Retry-After", "5")))
    except ValueError:
        return 5.0


def _error_reason(resp: requests.Response) -> str:
    try:
        error = resp.json().get("error", {})
        return str(error.get("reason") or error.get("message") or "")
    except (ValueError, AttributeError):
        return ""


def pick_cover_url(images: list | None, min_size: int = 160) -> str | None:
    """Smallest image that is still at least min_size px wide (falls back to the largest)."""
    usable = [i for i in images or [] if isinstance(i, dict) and i.get("url")]
    if not usable:
        return None
    usable.sort(key=lambda i: i.get("width") or 0)
    for image in usable:
        if (image.get("width") or 0) >= min_size:
            return image["url"]
    return usable[-1]["url"]


def playback_to_state(payload: dict | None, cover_path: str | None, now: float) -> MediaState:
    """Convert a /me/player response to MediaState."""
    if not payload or not payload.get("item"):
        return MediaState(source="webapi", player_available=bool(payload),
                          status=PlaybackStatus.STOPPED if payload else PlaybackStatus.NONE)
    item = payload["item"]
    if item.get("type") == "episode":
        artist = (item.get("show") or {}).get("name", "")
        album = artist
    else:
        artist = ", ".join(a.get("name", "") for a in item.get("artists") or [] if a.get("name"))
        album = (item.get("album") or {}).get("name", "")
    disallows = (payload.get("actions") or {}).get("disallows") or {}
    playing = bool(payload.get("is_playing"))
    device = payload.get("device") or {}
    volume = device.get("volume_percent")
    try:
        repeat = RepeatMode(payload.get("repeat_state"))
    except ValueError:
        repeat = None
    return MediaState(
        source="webapi",
        player_available=True,
        title=item.get("name", "") or "",
        artist=artist,
        album=album,
        status=PlaybackStatus.PLAYING if playing else PlaybackStatus.PAUSED,
        position_ms=payload.get("progress_ms"),
        duration_ms=item.get("duration_ms"),
        position_timestamp=now,
        cover_path=cover_path,
        caps=Capabilities(
            play=not disallows.get("resuming", False),
            pause=not disallows.get("pausing", False),
            next=not disallows.get("skipping_next", False),
            previous=not disallows.get("skipping_prev", False),
            seek=not disallows.get("seeking", False),
            repeat=repeat is not None and not (disallows.get("toggling_repeat_context", False)
                                               and disallows.get("toggling_repeat_track", False)),
        ),
        repeat=repeat,
        volume=volume / 100 if isinstance(volume, (int, float)) and device.get("supports_volume", True) else None,
    )


def cover_url_of(payload: dict | None) -> str | None:
    if not payload or not payload.get("item"):
        return None
    item = payload["item"]
    images = (item.get("album") or {}).get("images") or (item.get("images")) or (item.get("show") or {}).get("images")
    return pick_cover_url(images)


# --------------------------------------------------------------------------- worker
class SpotifyStatus(Enum):
    NOT_CONFIGURED = "not_configured"   # no Client ID
    DISCONNECTED = "disconnected"       # Client ID set, not authorized
    CONNECTING = "connecting"           # waiting for the browser
    CONNECTED = "connected"
    AUTH_REQUIRED = "auth_required"     # refresh token rejected
    ERROR = "error"                     # temporary problem (network, server)


class _ApiWorker(QObject):
    """Lives in its own QThread; every slot runs there."""

    state_ready = Signal(object)
    status_changed = Signal(str, str)
    command_done = Signal(str, bool, str)
    open_url = Signal(str)

    def __init__(self, store: TokenStore, cache: CoverCache | None) -> None:
        super().__init__()
        self._store = store
        self._cache = cache
        self._client: SpotifyApiClient | None = None
        self._client_id = ""
        self._port = 8765
        self._active = False
        self._timer: QTimer | None = None
        self._backoff_ms = 0
        self._cancel = threading.Event()
        self._last_payload_playing = False
        self._cover_url: str | None = None
        self._cover_path: str | None = None
        self._failed_cover_urls: set[str] = set()
        self._status = SpotifyStatus.NOT_CONFIGURED

    @Slot()
    def init(self) -> None:
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.poll)

    def _set_status(self, status: SpotifyStatus, message: str = "") -> None:
        self._status = status
        self.status_changed.emit(status.value, message)

    @Slot(str, int)
    def configure(self, client_id: str, port: int) -> None:
        if client_id == self._client_id and port == self._port and self._client is not None:
            return
        self._client_id, self._port = client_id, port
        if not client_id:
            self._client = None
            self._set_status(SpotifyStatus.NOT_CONFIGURED, "Укажите Client ID приложения Spotify")
            self._stop_timer()
            return
        self._client = SpotifyApiClient(client_id, redirect_uri_for(port), self._store)
        if self._client.authorized:
            self._set_status(SpotifyStatus.CONNECTED, "")
            self._schedule(0)
        else:
            self._set_status(SpotifyStatus.DISCONNECTED, "Spotify Web API не подключён")

    @Slot(bool)
    def set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        if active:
            self._schedule(0)
        else:
            self._stop_timer()

    @Slot()
    def connect_account(self) -> None:
        if self._client is None:
            self._set_status(SpotifyStatus.NOT_CONFIGURED, "Сначала укажите Client ID")
            return
        self._cancel.clear()
        self._stop_timer()
        verifier, challenge = make_pkce_pair()
        state = secrets.token_urlsafe(16)
        url = build_authorize_url(self._client_id, redirect_uri_for(self._port), state, challenge)
        self._set_status(SpotifyStatus.CONNECTING, "Подтвердите доступ в открывшемся браузере…")
        self.open_url.emit(url)
        try:
            code = wait_for_auth_code(self._port, state, self._cancel)
            self._client.exchange_code(code, verifier)
            profile = self._client.get_profile()
            name = profile.get("display_name") or profile.get("id") or ""
            self._set_status(SpotifyStatus.CONNECTED, f"Подключено: {name}" if name else "Подключено")
            self._backoff_ms = 0
            self._schedule(0)
        except SpotifyApiError as exc:
            log.warning("Spotify connect failed: %s", exc.message)
            status = SpotifyStatus.DISCONNECTED if not self._client.authorized else SpotifyStatus.CONNECTED
            self._set_status(status, exc.message)
            if self._client.authorized:
                self._schedule(0)

    @Slot()
    def disconnect_account(self) -> None:
        self._cancel.set()
        if self._client is not None:
            self._client.logout()
        self._stop_timer()
        self.state_ready.emit(MediaState(source="webapi"))
        if self._client_id:
            self._set_status(SpotifyStatus.DISCONNECTED, "Spotify Web API отключён")

    @Slot()
    def check_connection(self) -> None:
        if self._client is None or not self._client.authorized:
            self._set_status(self._status if self._client is None else SpotifyStatus.DISCONNECTED,
                             "Spotify Web API не подключён")
            return
        try:
            profile = self._client.get_profile()
            name = profile.get("display_name") or profile.get("id") or ""
            self._set_status(SpotifyStatus.CONNECTED, f"Соединение в порядке: {name}")
        except SpotifyApiError as exc:
            self._handle_error(exc)

    @Slot(str)
    def command(self, value: str) -> None:
        command = Command(value)
        if self._client is None or not self._client.authorized:
            self.command_done.emit(value, False, "Spotify Web API не подключён")
            return
        try:
            self._client.command(command, self._last_payload_playing)
            self.command_done.emit(value, True, "")
            self._schedule(400)
        except SpotifyApiError as exc:
            self.command_done.emit(value, False, exc.message)
            if exc.kind is ApiErrorKind.AUTH_REQUIRED:
                self._handle_error(exc)

    @Slot(str, int)
    def adjust(self, kind: str, value: int) -> None:
        """kind = "seek" (ms), "volume" (percent) or "repeat" (index in RepeatMode)."""
        if self._client is None or not self._client.authorized:
            self.command_done.emit(kind, False, "Spotify Web API не подключён")
            return
        try:
            if kind == "seek":
                self._client.seek(value)
            elif kind == "repeat":
                self._client.set_repeat(list(RepeatMode)[value].value)
            else:
                self._client.set_volume(value)
            self.command_done.emit(kind, True, "")
            self._schedule(400)
        except SpotifyApiError as exc:
            self.command_done.emit(kind, False, exc.message)
            if exc.kind is ApiErrorKind.AUTH_REQUIRED:
                self._handle_error(exc)

    @Slot()
    def poll(self) -> None:
        if not self._active or self._client is None or not self._client.authorized:
            return
        try:
            payload = self._client.get_playback()
        except SpotifyApiError as exc:
            self._handle_error(exc)
            return
        self._backoff_ms = 0
        if self._status is not SpotifyStatus.CONNECTED:
            self._set_status(SpotifyStatus.CONNECTED, "")
        self._last_payload_playing = bool(payload and payload.get("is_playing"))
        cover = self._resolve_cover(cover_url_of(payload))
        self.state_ready.emit(playback_to_state(payload, cover, time.monotonic()))
        self._schedule(POLL_PLAYING_MS if self._last_payload_playing else POLL_IDLE_MS)

    def _resolve_cover(self, url: str | None) -> str | None:
        if url is None or self._cache is None:
            return None
        if url == self._cover_url:
            return self._cover_path
        path = self._cache.path_for_url(url)
        if path is None and url not in self._failed_cover_urls and self._client is not None:
            try:
                path = self._cache.store_url(url, self._client.download(url))
            except SpotifyApiError as exc:
                log.debug("Cover download failed: %s", exc.message)
                self._failed_cover_urls.add(url)
        self._cover_url, self._cover_path = url, str(path) if path else None
        return self._cover_path

    def _handle_error(self, exc: SpotifyApiError) -> None:
        if exc.kind is ApiErrorKind.AUTH_REQUIRED:
            self._set_status(SpotifyStatus.AUTH_REQUIRED, exc.message)
            self.state_ready.emit(MediaState(source="webapi"))
            self._stop_timer()
            return
        if exc.kind is ApiErrorKind.RATE_LIMITED:
            delay = int((exc.retry_after or 5) * 1000)
            log.info("Spotify rate limit; next request in %.0fs", delay / 1000)
        else:
            self._backoff_ms = min(max(self._backoff_ms * 2, 5000), MAX_BACKOFF_MS)
            delay = self._backoff_ms
            log.info("Spotify API problem (%s); retry in %.0fs", exc.message, delay / 1000)
            self._set_status(SpotifyStatus.ERROR, exc.message)
        self._schedule(delay)

    def _schedule(self, delay_ms: int) -> None:
        if self._timer is not None and self._active:
            self._timer.start(delay_ms)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def cancel_blocking(self) -> None:
        """Thread-safe: abort a pending browser authorization."""
        self._cancel.set()

    @Slot()
    def shutdown(self) -> None:
        self._stop_timer()


class SpotifyWebApiSource(MediaSource):
    name = "webapi"

    status_changed = Signal(str, str)
    open_url = Signal(str)

    _configure = Signal(str, int)
    _set_active = Signal(bool)
    _connect = Signal()
    _disconnect = Signal()
    _check = Signal()
    _command = Signal(str)
    _adjust = Signal(str, int)
    _shutdown = Signal()

    def __init__(self, store: TokenStore, cache: CoverCache | None) -> None:
        super().__init__()
        self.status = SpotifyStatus.NOT_CONFIGURED
        self.status_message = ""
        self._thread = QThread()
        self._thread.setObjectName("spotify-webapi")
        self._worker = _ApiWorker(store, cache)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.init)
        self._thread.finished.connect(self._worker.deleteLater)

        self._configure.connect(self._worker.configure)
        self._set_active.connect(self._worker.set_active)
        self._connect.connect(self._worker.connect_account)
        self._disconnect.connect(self._worker.disconnect_account)
        self._check.connect(self._worker.check_connection)
        self._command.connect(self._worker.command)
        self._adjust.connect(self._worker.adjust)
        self._shutdown.connect(self._worker.shutdown)

        self._worker.state_ready.connect(self.state_changed)
        self._worker.command_done.connect(self.command_finished)
        self._worker.status_changed.connect(self._on_status)
        self._worker.open_url.connect(self.open_url)

    def _on_status(self, status: str, message: str) -> None:
        self.status = SpotifyStatus(status)
        self.status_message = message
        self.status_changed.emit(status, message)

    @property
    def connected(self) -> bool:
        return self.status in (SpotifyStatus.CONNECTED, SpotifyStatus.ERROR)

    def start(self) -> None:
        if not self._thread.isRunning():
            self._thread.start(QThread.Priority.LowPriority)

    def configure(self, client_id: str, port: int) -> None:
        self._configure.emit(client_id, port)

    def set_active(self, active: bool) -> None:
        self._set_active.emit(active)

    def connect_account(self) -> None:
        self._worker.cancel_blocking()     # abort a previous pending authorization, if any
        self._connect.emit()

    def disconnect_account(self) -> None:
        self._worker.cancel_blocking()
        self._disconnect.emit()

    def check_connection(self) -> None:
        self._check.emit()

    def send_command(self, command: Command) -> None:
        self._command.emit(command.value)

    def seek(self, position_ms: int) -> None:
        self._adjust.emit("seek", int(position_ms))

    def set_volume(self, level: float) -> None:
        self._adjust.emit("volume", round(min(1.0, max(0.0, level)) * 100))

    def set_repeat(self, mode: RepeatMode) -> None:
        self._adjust.emit("repeat", list(RepeatMode).index(mode))

    def stop(self, timeout_ms: int = 3000) -> bool:
        if not self._thread.isRunning():
            return True
        self._worker.cancel_blocking()
        self._shutdown.emit()
        self._thread.quit()
        finished = self._thread.wait(timeout_ms)
        if not finished:
            log.warning("Spotify API worker did not stop in time")
        return finished

    def is_running(self) -> bool:
        return self._thread.isRunning()
