"""System tray icon and its context menu."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class TrayController(QObject):
    toggle_overlay = Signal()
    open_settings = Signal()
    connect_spotify = Signal()
    toggle_game_mode = Signal(bool)
    toggle_autostart = Signal(bool)
    quit_requested = Signal()

    def __init__(self, icon: QIcon, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.menu = QMenu()
        self.action_overlay = QAction("Скрыть Dynamic Island", self.menu)
        self.action_settings = QAction("Настройки…", self.menu)
        self.action_spotify = QAction("Подключить Spotify Web API…", self.menu)
        self.action_game_mode = QAction("Игровой режим (скрыть)", self.menu, checkable=True)
        self.action_autostart = QAction("Запускать вместе с Windows", self.menu, checkable=True)
        self.action_quit = QAction("Выход", self.menu)

        self.action_overlay.triggered.connect(self.toggle_overlay)
        self.action_settings.triggered.connect(self.open_settings)
        self.action_spotify.triggered.connect(self.connect_spotify)
        self.action_game_mode.toggled.connect(self.toggle_game_mode)
        self.action_autostart.toggled.connect(self.toggle_autostart)
        self.action_quit.triggered.connect(self.quit_requested)

        for action in (self.action_overlay, self.action_settings, self.action_spotify):
            self.menu.addAction(action)
        self.menu.addSeparator()
        self.menu.addAction(self.action_game_mode)
        self.menu.addAction(self.action_autostart)
        self.menu.addSeparator()
        self.menu.addAction(self.action_quit)

        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Dynamic Island")
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_settings.emit()

    def show(self) -> bool:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        self.tray.show()
        return True

    def hide(self) -> None:
        self.tray.hide()

    def update_state(self, *, overlay_visible: bool, game_mode: bool, autostart: bool,
                     spotify_connected: bool) -> None:
        self.action_overlay.setText("Скрыть Dynamic Island" if overlay_visible else "Показать Dynamic Island")
        self.action_spotify.setText("Переподключить Spotify Web API…" if spotify_connected
                                    else "Подключить Spotify Web API…")
        for action, value in ((self.action_game_mode, game_mode), (self.action_autostart, autostart)):
            if action.isChecked() != value:
                action.blockSignals(True)
                action.setChecked(value)
                action.blockSignals(False)
