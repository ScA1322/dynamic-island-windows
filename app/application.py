"""Wires settings, media sources, the overlay, game detection, tray and settings window together."""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication

from app.autostart import AutostartManager
from app.config import (
    Settings, covers_dir, load_settings, read_env_file, save_settings, settings_path, token_path,
)
from app.game_detector import DetectorConfig, GameDetector, is_fullscreen
from app.island_window import IslandWindow
from app.media.controller import MediaController
from app.media.cover_cache import CoverCache
from app.media.models import Command, MediaState
from app.media.smtc import SmtcSource
from app.media.volume import SpotifyAudioVolume
from app.media.spotify_api import SpotifyStatus, SpotifyWebApiSource, TokenStore
from app.resources import app_icon
from app.settings_window import SettingsWindow
from app.system_tray import TrayController

log = logging.getLogger(__name__)


def env_client_id() -> str:
    """SPOTIFY_CLIENT_ID from the environment or a .env file next to the app (optional)."""
    value = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    if value:
        return value
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    return read_env_file(base / ".env").get("SPOTIFY_CLIENT_ID", "").strip()


def detector_config(s: Settings) -> DetectorConfig:
    return DetectorConfig(
        hide_in_games=s.hide_in_games,
        hide_in_fullscreen=s.hide_in_fullscreen,
        manual_game_mode=s.game_mode_manual,
        excluded=frozenset(s.excluded_processes),
        games=frozenset(s.game_processes),
    )


class IslandApplication(QObject):
    def __init__(self, qapp: QApplication, *, settings_file: Path | None = None,
                 media: MediaController | None = None, detector: GameDetector | None = None,
                 autostart: AutostartManager | None = None) -> None:
        super().__init__()
        self.qapp = qapp
        self.settings_file = settings_file or settings_path()
        self.settings = load_settings(self.settings_file)
        if not self.settings.spotify_client_id:
            client_id = env_client_id()
            if client_id:
                self.settings = replace(self.settings, spotify_client_id=client_id).validated()
        self._shut_down = False

        self.icon = app_icon()
        if media is None:
            cache = CoverCache(covers_dir())
            media = MediaController(SmtcSource(cache), SpotifyWebApiSource(TokenStore(token_path()), cache),
                                    SpotifyAudioVolume())
        self.media = media
        self.window = IslandWindow(self.settings)
        self.detector = detector or GameDetector(config=detector_config(self.settings),
                                                 island_hwnd_fn=lambda: int(self.window.winId()))
        self.detector.config = detector_config(self.settings)
        self.autostart = autostart or AutostartManager()
        self.tray = TrayController(self.icon, self)
        self.settings_window = SettingsWindow(self.settings, self.icon)

        self._diag_timer = QTimer(self)
        self._diag_timer.setInterval(1000)
        self._diag_timer.timeout.connect(self._update_diagnostics)

        self._wire()

    # ------------------------------------------------------------------ wiring
    def _wire(self) -> None:
        m, w, t, sw = self.media, self.window, self.tray, self.settings_window
        m.state_changed.connect(self._on_media_state)
        m.notice.connect(w.show_notice)
        w.command_requested.connect(self._on_command)
        w.seek_requested.connect(m.seek)
        w.volume_requested.connect(m.set_volume)
        w.repeat_requested.connect(m.set_repeat)
        w.volume_provider = m.volume
        self.detector.hidden_changed.connect(lambda *_: self._update_visibility())

        t.toggle_overlay.connect(self.toggle_overlay)
        t.open_settings.connect(self.show_settings)
        t.connect_spotify.connect(self._tray_connect_spotify)
        t.toggle_game_mode.connect(lambda on: self.update_settings(game_mode_manual=on))
        t.toggle_autostart.connect(lambda on: self.update_settings(autostart=on))
        t.quit_requested.connect(self.quit)

        sw.settings_applied.connect(self.apply_settings)
        sw.toggle_overlay.connect(self.toggle_overlay)
        sw.quit_requested.connect(self.quit)

        api = self.media.webapi
        if api is not None:
            sw.spotify_connect.connect(api.connect_account)
            sw.spotify_check.connect(api.check_connection)
            sw.spotify_reconnect.connect(api.connect_account)
            sw.spotify_disconnect.connect(api.disconnect_account)
            api.status_changed.connect(self._on_spotify_status)
            api.open_url.connect(lambda url: QDesktopServices.openUrl(QUrl(url)))

    def start(self) -> None:
        log.info("Starting; settings at %s", self.settings_file)
        self.autostart.sync(self.settings.autostart)
        self.media.start()
        if self.media.webapi is not None:
            self.media.webapi.configure(self.settings.spotify_client_id, self.settings.spotify_redirect_port)
        self.window.set_media_state(self.media.state)
        if self.media.local_volume is not None:
            available = self.media.local_volume.get() is not None
            log.info("Spotify volume control: %s", "available" if available else "not yet (no Spotify audio session)")
        self.detector.start()
        self._update_visibility(animated=False)
        if not self.tray.show():
            log.warning("System tray is unavailable; opening settings so the app stays reachable")
            self.show_settings()
        self._refresh_tray()
        self._diag_timer.start()

    # ------------------------------------------------------------------ behaviour
    def _on_media_state(self, state: MediaState) -> None:
        self.window.set_media_state(state)
        self._update_source_label()

    def _on_command(self, command: Command) -> None:
        self.media.send_command(command)

    def _update_visibility(self, animated: bool = True) -> None:
        visible = self.settings.overlay_visible and not self.detector.hidden
        self.window.set_overlay_visible(visible, animated=animated)

    def toggle_overlay(self) -> None:
        self.update_settings(overlay_visible=not self.settings.overlay_visible)

    def update_settings(self, **changes) -> None:
        self.apply_settings(replace(self.settings, **changes))

    def apply_settings(self, new: Settings) -> None:
        new = new.validated()
        old, self.settings = self.settings, new
        try:
            save_settings(new, self.settings_file)
        except OSError as exc:
            log.error("Cannot save settings: %s", exc)
        if new.autostart != old.autostart or new.autostart:
            self.autostart.sync(new.autostart)
        self.window.apply_settings(new)
        self.detector.set_config(detector_config(new))
        if self.media.webapi is not None and (new.spotify_client_id, new.spotify_redirect_port) != (
                old.spotify_client_id, old.spotify_redirect_port):
            self.media.webapi.configure(new.spotify_client_id, new.spotify_redirect_port)
        self.settings_window.load(new)
        self._update_visibility()
        self._refresh_tray()

    def show_settings(self) -> None:
        self.settings_window.load(self.settings)
        self._update_source_label()
        api = self.media.webapi
        if api is not None:
            self.settings_window.set_spotify_status(api.status, api.status_message)
        self._update_diagnostics()
        self.settings_window.present()

    def _tray_connect_spotify(self) -> None:
        if not self.settings.spotify_client_id:
            self.show_settings()
            self.settings_window.set_spotify_status(SpotifyStatus.NOT_CONFIGURED,
                                                    "Укажите Client ID на вкладке Spotify")
            return
        self.media.webapi.connect_account()

    def _on_spotify_status(self, status: str, message: str) -> None:
        self.settings_window.set_spotify_status(SpotifyStatus(status), message)
        self._refresh_tray()
        if status == SpotifyStatus.AUTH_REQUIRED.value:
            self.window.show_notice("Spotify: нужна повторная авторизация")

    def _refresh_tray(self) -> None:
        api = self.media.webapi
        self.tray.update_state(
            overlay_visible=self.settings.overlay_visible,
            game_mode=self.settings.game_mode_manual,
            autostart=self.settings.autostart,
            spotify_connected=bool(api is not None and api.connected),
        )
        self.settings_window.set_overlay_visible(self.settings.overlay_visible)

    def _update_source_label(self) -> None:
        state = self.media.state
        if state.source == "smtc":
            text = "системные медиа-элементы Windows (SMTC)"
        elif state.source == "webapi":
            text = "Spotify Web API"
        else:
            text = "нет — Spotify не найден"
        self.settings_window.set_source(text)

    def _update_diagnostics(self) -> None:
        if not self.settings_window.isVisible():
            return
        info = self.detector.last_info
        decision = self.detector.last_decision
        if info is None:
            text = f"Решение: {decision.reason}"
        else:
            text = (f"Активное окно: {info.process_name or '?'} [{info.class_name}]\n"
                    f"Полноэкранное: {'да' if is_fullscreen(info) else 'нет'}\n"
                    f"Решение: {'скрыть' if self.detector.hidden else 'показать'} — {decision.reason}")
        self.settings_window.set_detector_status(text)

    # ------------------------------------------------------------------ shutdown
    def shutdown(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        log.info("Shutting down")
        self._diag_timer.stop()
        self.detector.stop()
        self.window.shutdown()
        self.settings_window.hide()
        stopped = self.media.stop()
        self.tray.hide()
        try:
            save_settings(self.settings, self.settings_file)
        except OSError as exc:
            log.error("Cannot save settings: %s", exc)
        log.info("Shutdown complete (workers stopped: %s)", stopped)

    def quit(self) -> None:
        self.shutdown()
        self.qapp.quit()
