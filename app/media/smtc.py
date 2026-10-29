"""Windows System Media Transport Controls (SMTC) source.

Verified on Windows 11 with the Spotify desktop client: SMTC exposes title, artist, album,
cover art (PNG), playback status, timeline (position/duration) and next/previous/play/pause.
Spotify updates the timeline only on events (seek, pause...), so the UI extrapolates position.

Everything WinRT runs in a dedicated QThread with its own asyncio loop. SMTC change events
wake the loop; a slow poll is kept as a safety net for missed events.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
import time
from typing import Callable

from PySide6.QtCore import QThread, Signal

from app.media.base import MediaSource
from app.media.cover_cache import MAX_COVER_BYTES, CoverCache
from app.media.models import Capabilities, Command, MediaState, PlaybackStatus, RepeatMode

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0
MAX_COVER_ATTEMPTS = 4

# GlobalSystemMediaTransportControlsSessionPlaybackStatus
_STATUS = {
    0: PlaybackStatus.STOPPED,   # Closed
    1: PlaybackStatus.STOPPED,   # Opened
    2: PlaybackStatus.PAUSED,    # Changing
    3: PlaybackStatus.STOPPED,   # Stopped
    4: PlaybackStatus.PLAYING,
    5: PlaybackStatus.PAUSED,
}


# Windows.Media.MediaPlaybackAutoRepeatMode
_REPEAT = {0: RepeatMode.OFF, 1: RepeatMode.TRACK, 2: RepeatMode.CONTEXT}
_REPEAT_VALUE = {mode: value for value, mode in _REPEAT.items()}


def map_repeat(value) -> RepeatMode | None:
    if value is None:
        return None
    try:
        return _REPEAT.get(int(value))
    except (TypeError, ValueError):
        return None


def is_spotify(app_id: str) -> bool:
    # desktop build: "Spotify.exe"; Microsoft Store build: "SpotifyAB.SpotifyMusic_...!Spotify"
    return "spotify" in (app_id or "").lower()


def map_status(value) -> PlaybackStatus:
    try:
        return _STATUS.get(int(value), PlaybackStatus.STOPPED)
    except (TypeError, ValueError):
        return PlaybackStatus.STOPPED


def timeline_to_position(position: dt.timedelta | None, end: dt.timedelta | None,
                         updated: dt.datetime | None, playing: bool,
                         now_utc: dt.datetime | None = None) -> tuple[int | None, int | None]:
    """Convert SMTC timeline values to (position_ms "now", duration_ms)."""
    duration_ms = int(end.total_seconds() * 1000) if end and end.total_seconds() > 0 else None
    if position is None or duration_ms is None:
        return None, duration_ms
    pos_ms = int(position.total_seconds() * 1000)
    if playing and updated is not None and updated.year > 1700:
        now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=dt.timezone.utc)
        elapsed = (now_utc - updated).total_seconds()
        if 0 < elapsed < 24 * 3600:
            pos_ms += int(elapsed * 1000)
    return max(0, min(pos_ms, duration_ms)), duration_ms


class _SmtcThread(QThread):
    state_ready = Signal(object)
    command_done = Signal(str, bool, str)

    def __init__(self, cover_cache: CoverCache | None, app_filter: Callable[[str], bool],
                 poll_interval: float = POLL_INTERVAL_S) -> None:
        super().__init__()
        self.setObjectName("smtc-worker")
        self._cache = cover_cache
        self._filter = app_filter
        self._poll_interval = poll_interval
        self._stop_flag = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._session = None
        self._session_id: str | None = None
        self._tokens: list[tuple[str, object]] = []
        self._sessions_dirty = True
        self._cover_key: tuple | None = None
        self._cover_path: str | None = None
        self._cover_attempts = 0
        self._last_state: MediaState | None = None

    # ------------------------------------------------------------- thread-safe API
    def request_stop(self) -> None:
        self._stop_flag.set()
        self.poke()

    def poke(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass

    def submit(self, command: Command) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self.command_done.emit(command.value, False, "Системные медиа-элементы ещё не готовы")
            return
        asyncio.run_coroutine_threadsafe(self._run_command(command), loop)

    def submit_seek(self, position_ms: int) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self.command_done.emit("seek", False, "Системные медиа-элементы ещё не готовы")
            return
        asyncio.run_coroutine_threadsafe(self._run_seek(position_ms), loop)

    def submit_repeat(self, mode: RepeatMode) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self.command_done.emit("repeat", False, "Системные медиа-элементы ещё не готовы")
            return
        asyncio.run_coroutine_threadsafe(self._run_repeat(mode), loop)

    # ------------------------------------------------------------- thread body
    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception:
            log.exception("SMTC worker crashed")
        finally:
            self._loop = None
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    async def _main(self) -> None:
        self._wake = asyncio.Event()
        try:
            # imported here so the MTA apartment is initialised in this thread, not Qt's STA thread
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager,
            )
        except Exception as exc:
            log.error("WinRT media control is unavailable: %s", exc)
            self._emit(MediaState(source="smtc"))
            return

        manager = None
        backoff = 1.0
        while not self._stop_flag.is_set():
            if manager is None:
                try:
                    manager = await Manager.request_async()
                    manager.add_sessions_changed(lambda *_: self._mark_sessions_dirty())
                    manager.add_current_session_changed(lambda *_: self._mark_sessions_dirty())
                    backoff = 1.0
                    log.info("SMTC session manager ready")
                except Exception as exc:
                    log.warning("SMTC manager unavailable (%s); retrying in %.0fs", exc, backoff)
                    await self._sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
            try:
                await self._refresh(manager)
            except Exception as exc:
                log.warning("SMTC refresh failed: %s", exc)
                self._drop_session()
            await self._sleep(self._poll_interval)
        self._drop_session()

    async def _sleep(self, seconds: float) -> None:
        assert self._wake is not None
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    def _mark_sessions_dirty(self) -> None:
        self._sessions_dirty = True
        self.poke()

    # ------------------------------------------------------------- session handling
    def _pick_session(self, manager):
        for session in manager.get_sessions():
            try:
                if self._filter(session.source_app_user_model_id):
                    return session
            except Exception:
                continue
        return None

    def _drop_session(self) -> None:
        session, tokens = self._session, self._tokens
        self._session, self._session_id, self._tokens = None, None, []
        if session is None:
            return
        for name, token in tokens:
            try:
                getattr(session, f"remove_{name}")(token)
            except Exception:
                pass

    def _attach_session(self, session) -> None:
        self._drop_session()
        self._session = session
        self._session_id = session.source_app_user_model_id
        for name in ("media_properties_changed", "playback_info_changed", "timeline_properties_changed"):
            try:
                token = getattr(session, f"add_{name}")(lambda *_: self.poke())
                self._tokens.append((name, token))
            except Exception as exc:
                log.debug("Cannot subscribe to %s: %s", name, exc)
        log.info("Tracking SMTC session %s", self._session_id)

    async def _refresh(self, manager) -> None:
        if self._sessions_dirty or self._session is None:
            self._sessions_dirty = False
            session = self._pick_session(manager)
            if session is None:
                if self._session is not None:
                    log.info("Spotify SMTC session closed")
                self._drop_session()
            elif session.source_app_user_model_id != self._session_id or self._session is None:
                self._attach_session(session)

        session = self._session
        if session is None:
            self._emit(MediaState(source="smtc", player_available=False))
            return

        props = await session.try_get_media_properties_async()
        playback = session.get_playback_info()
        timeline = session.get_timeline_properties()

        status = map_status(playback.playback_status) if playback else PlaybackStatus.STOPPED
        controls = playback.controls if playback else None
        caps = Capabilities(
            play=bool(controls and (controls.is_play_enabled or controls.is_play_pause_toggle_enabled)),
            pause=bool(controls and (controls.is_pause_enabled or controls.is_play_pause_toggle_enabled)),
            next=bool(controls and controls.is_next_enabled),
            previous=bool(controls and controls.is_previous_enabled),
            seek=bool(controls and controls.is_playback_position_enabled),
            repeat=bool(controls and controls.is_repeat_enabled),
        )
        repeat = None
        if playback is not None and caps.repeat:
            try:
                repeat = map_repeat(playback.auto_repeat_mode)
            except Exception:
                repeat = None
        title = (props.title or "").strip() if props else ""
        artist = (props.artist or "").strip() if props else ""
        album = (props.album_title or "").strip() if props else ""

        position_ms = duration_ms = None
        if timeline is not None:
            try:
                position_ms, duration_ms = timeline_to_position(
                    timeline.position, timeline.end_time, timeline.last_updated_time,
                    status is PlaybackStatus.PLAYING)
            except Exception as exc:
                log.debug("Bad timeline: %s", exc)

        key = (title, artist, album)
        if key != self._cover_key:
            self._cover_key, self._cover_path, self._cover_attempts = key, None, 0
        if title and self._cover_path is None and self._cover_attempts < MAX_COVER_ATTEMPTS and props:
            self._cover_attempts += 1
            self._cover_path = await self._read_cover(props.thumbnail)

        self._emit(MediaState(
            source="smtc", player_available=True, title=title, artist=artist, album=album,
            status=status if title else PlaybackStatus.STOPPED,
            position_ms=position_ms, duration_ms=duration_ms, position_timestamp=time.monotonic(),
            cover_path=self._cover_path, caps=caps, repeat=repeat,
        ))

    async def _read_cover(self, thumbnail) -> str | None:
        if thumbnail is None or self._cache is None:
            return None
        try:
            from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions

            stream = await thumbnail.open_read_async()
            size = int(stream.size)
            if size <= 0 or size > MAX_COVER_BYTES:
                return None
            buffer = Buffer(size)
            await stream.read_async(buffer, buffer.capacity, InputStreamOptions.READ_AHEAD)
            reader = DataReader.from_buffer(buffer)
            data = bytearray(buffer.length)
            reader.read_bytes(data)
            try:
                stream.close()
            except Exception:
                pass
            path = self._cache.store_bytes(bytes(data))
            return str(path) if path else None
        except Exception as exc:
            log.debug("Cannot read SMTC thumbnail: %s", exc)
            return None

    def _emit(self, state: MediaState) -> None:
        if state.same_as(self._last_state):
            return
        self._last_state = state
        self.state_ready.emit(state)

    # ------------------------------------------------------------- commands
    async def _run_command(self, command: Command) -> None:
        session = self._session
        if session is None:
            self.command_done.emit(command.value, False, "Spotify не найден в системных медиа-элементах")
            return
        try:
            if command is Command.NEXT:
                ok = await session.try_skip_next_async()
            elif command is Command.PREVIOUS:
                ok = await session.try_skip_previous_async()
            elif command is Command.PLAY:
                ok = await session.try_play_async()
            elif command is Command.PAUSE:
                ok = await session.try_pause_async()
            else:
                ok = await session.try_toggle_play_pause_async()
            ok = bool(ok)
            self.command_done.emit(command.value, ok, "" if ok else "Spotify отклонил команду")
        except Exception as exc:
            log.warning("SMTC command %s failed: %s", command.value, exc)
            self.command_done.emit(command.value, False, "Команда недоступна")
        # status/track change usually lands within a few hundred ms; refresh promptly
        await asyncio.sleep(0.25)
        self.poke()


    async def _run_seek(self, position_ms: int) -> None:
        session = self._session
        if session is None:
            self.command_done.emit("seek", False, "Spotify не найден в системных медиа-элементах")
            return
        try:
            # TimeSpan ticks = 100 ns
            ok = bool(await session.try_change_playback_position_async(max(0, int(position_ms)) * 10_000))
            self.command_done.emit("seek", ok, "" if ok else "Spotify отклонил перемотку")
        except Exception as exc:
            log.warning("SMTC seek failed: %s", exc)
            self.command_done.emit("seek", False, "Перемотка недоступна")
        await asyncio.sleep(0.25)
        self.poke()


    async def _run_repeat(self, mode: RepeatMode) -> None:
        session = self._session
        if session is None:
            self.command_done.emit("repeat", False, "Spotify не найден в системных медиа-элементах")
            return
        try:
            from winrt.windows.media import MediaPlaybackAutoRepeatMode

            ok = bool(await session.try_change_auto_repeat_mode_async(
                MediaPlaybackAutoRepeatMode(_REPEAT_VALUE[mode])))
            self.command_done.emit("repeat", ok, "" if ok else "Spotify отклонил смену повтора")
        except Exception as exc:
            log.warning("SMTC repeat failed: %s", exc)
            self.command_done.emit("repeat", False, "Повтор недоступен")
        await asyncio.sleep(0.25)
        self.poke()


class SmtcSource(MediaSource):
    name = "smtc"

    def __init__(self, cover_cache: CoverCache | None,
                 app_filter: Callable[[str], bool] = is_spotify,
                 poll_interval: float = POLL_INTERVAL_S) -> None:
        super().__init__()
        self._thread = _SmtcThread(cover_cache, app_filter, poll_interval)
        self._thread.state_ready.connect(self.state_changed)
        self._thread.command_done.connect(self.command_finished)

    def start(self) -> None:
        if not self._thread.isRunning():
            self._thread.start(QThread.Priority.LowPriority)

    def stop(self, timeout_ms: int = 3000) -> bool:
        if not self._thread.isRunning():
            return True
        self._thread.request_stop()
        finished = self._thread.wait(timeout_ms)
        if not finished:
            log.warning("SMTC worker did not stop in time")
        return finished

    def is_running(self) -> bool:
        return self._thread.isRunning()

    def send_command(self, command: Command) -> None:
        self._thread.submit(command)

    def seek(self, position_ms: int) -> None:
        self._thread.submit_seek(position_ms)

    def set_repeat(self, mode: RepeatMode) -> None:
        self._thread.submit_repeat(mode)

    def refresh(self) -> None:
        self._thread.poke()
