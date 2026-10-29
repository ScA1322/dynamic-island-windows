"""Chooses the active playback source and routes transport commands to it.

Priority: SMTC (local Spotify client, no network, no rate limits) -> Spotify Web API (only when
the user connected it and SMTC has no Spotify session, e.g. playback on another device).
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal

from app.media.base import MediaSource
from app.media.models import EMPTY_STATE, Command, MediaState, RepeatMode

log = logging.getLogger(__name__)


class MediaController(QObject):
    state_changed = Signal(object)        # MediaState
    notice = Signal(str)                  # short user-facing message (errors of commands etc.)

    def __init__(self, smtc: MediaSource | None, webapi=None, local_volume=None) -> None:
        super().__init__()
        self.smtc = smtc
        self.webapi = webapi
        self.local_volume = local_volume      # SpotifyAudioVolume (Windows mixer) for local playback
        self._smtc_state: MediaState = EMPTY_STATE
        self._api_state: MediaState = EMPTY_STATE
        self.state: MediaState = EMPTY_STATE
        self.active_source: MediaSource | None = None
        self._api_active = False
        for source in (smtc, webapi):
            if source is not None:
                source.command_finished.connect(self._on_command_finished)
        if smtc is not None:
            smtc.state_changed.connect(self._on_smtc_state)
        if webapi is not None:
            webapi.state_changed.connect(self._on_api_state)
            webapi.status_changed.connect(lambda *_: self._recompute())

    def start(self) -> None:
        for source in (self.smtc, self.webapi):
            if source is not None:
                source.start()

    def stop(self) -> bool:
        ok = True
        for source in (self.smtc, self.webapi):
            if source is not None:
                ok = source.stop() and ok
        return ok

    def _on_smtc_state(self, state: MediaState) -> None:
        self._smtc_state = state
        self._recompute()

    def _on_api_state(self, state: MediaState) -> None:
        self._api_state = state
        self._recompute()

    def _recompute(self) -> None:
        smtc_ok = self._smtc_state.player_available
        api_connected = bool(self.webapi is not None and self.webapi.connected)
        want_api = api_connected and not smtc_ok
        if want_api != self._api_active and self.webapi is not None:
            self._api_active = want_api
            self.webapi.set_active(want_api)
            log.info("Web API polling %s", "enabled" if want_api else "paused (SMTC has Spotify)")

        if smtc_ok:
            state, source = self._smtc_state, self.smtc
        elif want_api and self._api_state.player_available:
            state, source = self._api_state, self.webapi
        else:
            state, source = EMPTY_STATE, None

        self.active_source = source
        if not state.same_as(self.state):
            if state.track_key != self.state.track_key and state.has_track:
                log.info("Now playing via %s: %s — %s", state.source, state.artist, state.title)
            self.state = state
            self.state_changed.emit(state)

    def send_command(self, command: Command) -> None:
        source = self.active_source
        if source is None:
            self.notice.emit("Spotify не запущен")
            return
        if self.state.has_track and not self.state.caps.allows(command):
            self.notice.emit("Команда сейчас недоступна")
            return
        log.info("Command %s -> %s", command.value, source.name)
        source.send_command(command)

    def seek(self, position_ms: int) -> None:
        source = self.active_source
        if source is None or not self.state.has_track:
            return
        if not self.state.caps.seek:
            self.notice.emit("Перемотка сейчас недоступна")
            return
        log.info("Seek to %d ms -> %s", position_ms, source.name)
        source.seek(position_ms)

    def set_repeat(self, mode: RepeatMode) -> None:
        source = self.active_source
        if source is None or not self.state.has_track:
            return
        if not self.state.caps.repeat:
            self.notice.emit("Повтор сейчас недоступен")
            return
        log.info("Repeat %s -> %s", mode.value, source.name)
        source.set_repeat(mode)

    def volume(self) -> float | None:
        """Current Spotify volume 0..1, or None when it cannot be controlled."""
        source = self.active_source
        if source is None:
            return None
        if source is self.smtc:
            return self.local_volume.get() if self.local_volume is not None else None
        return self.state.volume

    def set_volume(self, level: float) -> None:
        source = self.active_source
        if source is None:
            return
        if source is self.smtc:
            if self.local_volume is None or not self.local_volume.set(level):
                self.notice.emit("Громкость Spotify недоступна")
        else:
            source.set_volume(level)

    def _on_command_finished(self, command: str, ok: bool, message: str) -> None:
        if not ok:
            log.info("Command %s failed: %s", command, message)
            self.notice.emit(message or "Команда не выполнена")
            if command in ("seek", "repeat"):       # undo the optimistic change shown by the UI
                self.state_changed.emit(self.state)
