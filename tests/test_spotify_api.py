import base64
import hashlib
import logging
import threading
import urllib.parse
import urllib.request

import pytest
import requests

from app.logger import RedactingFilter, redact
from app.media.models import Command, PlaybackStatus
from app.media.spotify_api import (
    ApiErrorKind, SpotifyApiClient, SpotifyApiError, Token, TokenStore, build_authorize_url,
    cover_url_of, make_pkce_pair, pick_cover_url, playback_to_state, wait_for_auth_code,
)


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, content=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.content = content if content is not None else (b"{}" if payload is not None else b"")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeHttp:
    """Scripted requests.Session replacement."""

    def __init__(self, api=None, token=None):
        self.api = list(api or [])
        self.token = list(token or [])
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("headers", {}).get("Authorization")))
        item = self.api.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, data=None, **kw):
        self.calls.append(("TOKEN", data.get("grant_type"), None))
        item = self.token.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kw):
        return self.request("GET", url, **kw)


class MemoryStore(TokenStore):
    def __init__(self, token=None):
        self.token = token
        self.cleared = False

    def load(self):
        return self.token

    def save(self, token):
        self.token = token

    def clear(self):
        self.token = None
        self.cleared = True


def client(http, token=None, now=1000.0):
    token = token or Token("old-access", "refresh-1", expires_at=now + 3600)
    return SpotifyApiClient("cid", "http://127.0.0.1:8765/callback", MemoryStore(token), session=http, clock=lambda: now)


PLAYBACK = {
    "is_playing": True,
    "progress_ms": 5000,
    "item": {
        "name": "Song", "duration_ms": 180000, "type": "track",
        "artists": [{"name": "A"}, {"name": "B"}],
        "album": {"name": "Alb", "images": [{"url": "https://i/640", "width": 640},
                                             {"url": "https://i/300", "width": 300},
                                             {"url": "https://i/64", "width": 64}]},
    },
    "actions": {"disallows": {"skipping_prev": True}},
}


def test_pkce_pair_is_valid():
    verifier, challenge = make_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_authorize_url_has_minimal_scopes():
    url = build_authorize_url("cid", "http://127.0.0.1:8765/callback", "st", "ch")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"] and q["state"] == ["st"]
    assert set(q["scope"][0].split()) == {"user-read-playback-state", "user-read-currently-playing",
                                          "user-modify-playback-state"}
    assert "client_secret" not in q


def test_playback_parsing():
    state = playback_to_state(PLAYBACK, "/c.png", now=1.0)
    assert state.title == "Song" and state.artist == "A, B" and state.album == "Alb"
    assert state.status is PlaybackStatus.PLAYING and state.duration_ms == 180000
    assert state.caps.next and not state.caps.previous
    assert cover_url_of(PLAYBACK) == "https://i/300"
    assert pick_cover_url([{"url": "https://x", "width": 64}]) == "https://x"
    assert pick_cover_url([]) is None
    empty = playback_to_state(None, None, 1.0)
    assert not empty.has_track and not empty.player_available


def test_get_playback_and_204():
    http = FakeHttp(api=[FakeResponse(200, PLAYBACK), FakeResponse(204)])
    c = client(http)
    assert c.get_playback()["item"]["name"] == "Song"
    assert c.get_playback() is None


def test_expired_token_is_refreshed_before_call():
    http = FakeHttp(api=[FakeResponse(200, {"id": "me"})],
                    token=[FakeResponse(200, {"access_token": "new-access", "expires_in": 3600})])
    c = client(http, Token("old", "refresh-1", expires_at=500), now=1000)
    assert c.get_profile() == {"id": "me"}
    assert http.calls[0][1] == "refresh_token"
    assert http.calls[1][2] == "Bearer new-access"
    assert c.token.refresh_token == "refresh-1"          # kept when Spotify does not rotate it


def test_401_triggers_single_refresh_and_retry():
    http = FakeHttp(api=[FakeResponse(401), FakeResponse(200, {"id": "me"})],
                    token=[FakeResponse(200, {"access_token": "fresh", "refresh_token": "refresh-2"})])
    c = client(http)
    assert c.get_profile() == {"id": "me"}
    assert c.token.refresh_token == "refresh-2"


def test_revoked_refresh_token_requires_reauth():
    http = FakeHttp(api=[FakeResponse(401)], token=[FakeResponse(400, {"error": "invalid_grant"})])
    c = client(http)
    with pytest.raises(SpotifyApiError) as err:
        c.get_playback()
    assert err.value.kind is ApiErrorKind.AUTH_REQUIRED
    assert c.token is None and c.store.cleared


def test_rate_limit_reports_retry_after():
    c = client(FakeHttp(api=[FakeResponse(429, headers={"Retry-After": "12"})]))
    with pytest.raises(SpotifyApiError) as err:
        c.get_playback()
    assert err.value.kind is ApiErrorKind.RATE_LIMITED and err.value.retry_after == 12


def test_premium_required_is_reported_not_bypassed():
    c = client(FakeHttp(api=[FakeResponse(403, {"error": {"status": 403, "reason": "PREMIUM_REQUIRED"}})]))
    with pytest.raises(SpotifyApiError) as err:
        c.command(Command.NEXT, currently_playing=True)
    assert err.value.kind is ApiErrorKind.PREMIUM_REQUIRED


def test_no_active_device_and_server_and_network_errors():
    c = client(FakeHttp(api=[FakeResponse(404), FakeResponse(503), requests.ConnectionError("down")]))
    kinds = []
    for _ in range(3):
        with pytest.raises(SpotifyApiError) as err:
            c.command(Command.PLAY, currently_playing=False)
        kinds.append(err.value.kind)
    assert kinds == [ApiErrorKind.NO_ACTIVE_DEVICE, ApiErrorKind.SERVER, ApiErrorKind.NETWORK]


def test_toggle_maps_to_pause_or_play():
    http = FakeHttp(api=[FakeResponse(204), FakeResponse(204)])
    c = client(http)
    c.command(Command.TOGGLE, currently_playing=True)
    c.command(Command.TOGGLE, currently_playing=False)
    assert [(m, u.rsplit("/", 1)[-1]) for m, u, _ in http.calls] == [("PUT", "pause"), ("PUT", "play")]


def test_cover_download_only_https():
    c = client(FakeHttp())
    with pytest.raises(SpotifyApiError):
        c.download("http://insecure/x.jpg")


def test_token_store_dpapi_roundtrip(tmp_path):
    store = TokenStore(tmp_path / "t.bin")
    store.save(Token("access-secret", "refresh-secret", 123.0, "scope"))
    raw = (tmp_path / "t.bin").read_bytes()
    assert b"access-secret" not in raw and b"refresh-secret" not in raw     # encrypted at rest
    loaded = store.load()
    assert loaded.access_token == "access-secret" and loaded.expires_at == 123.0
    (tmp_path / "t.bin").write_bytes(b"garbage")
    assert store.load() is None
    store.clear()
    assert not (tmp_path / "t.bin").exists()


def _free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("query,expected", [("code=abc&state=S", "abc"), ("code=abc&state=WRONG", None),
                                            ("error=access_denied&state=S", None)])
def test_loopback_callback(query, expected):
    port = _free_port()
    result = {}

    def run():
        try:
            result["code"] = wait_for_auth_code(port, "S", threading.Event(), timeout_s=5)
        except SpotifyApiError as exc:
            result["error"] = exc.message

    thread = threading.Thread(target=run)
    thread.start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?{query}", timeout=2).read()
            break
        except OSError:
            threading.Event().wait(0.05)
    thread.join(5)
    assert result.get("code") == expected
    if expected is None:
        assert "error" in result


def test_auth_wait_can_be_cancelled():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(SpotifyApiError):
        wait_for_auth_code(_free_port(), "S", cancel, timeout_s=5)


def test_secrets_are_redacted_in_logs(caplog):
    assert "abc.def" not in redact("Authorization: Bearer abc.def")
    assert "XYZ" not in redact('{"access_token": "XYZ", "refresh_token": "XYZ"}')
    assert "QQQ" not in redact("code=QQQ&state=1")
    logger = logging.getLogger("redact-test")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="redact-test"):
        logger.info("token %s", "refresh_token=SECRET123")
    assert "SECRET123" not in caplog.text
