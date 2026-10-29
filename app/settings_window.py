"""Settings window. Closing it only hides it; the application keeps running in the tray."""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from app.config import LIMITS, Settings
from app.media.spotify_api import SpotifyStatus, redirect_uri_for

_STATUS_TEXT = {
    SpotifyStatus.NOT_CONFIGURED: "не настроен (нет Client ID)",
    SpotifyStatus.DISCONNECTED: "не подключён",
    SpotifyStatus.CONNECTING: "ожидание авторизации в браузере…",
    SpotifyStatus.CONNECTED: "подключён",
    SpotifyStatus.AUTH_REQUIRED: "требуется повторное подключение",
    SpotifyStatus.ERROR: "временная ошибка",
}


def _spin(name: str, suffix: str = " px", step: int = 10) -> QSpinBox:
    box = QSpinBox()
    lo, hi = LIMITS[name]
    box.setRange(lo, hi)
    box.setSingleStep(step)
    box.setSuffix(suffix)
    return box


class SettingsWindow(QWidget):
    settings_applied = Signal(object)       # Settings
    toggle_overlay = Signal()
    quit_requested = Signal()
    spotify_connect = Signal()
    spotify_check = Signal()
    spotify_reconnect = Signal()
    spotify_disconnect = Signal()

    def __init__(self, settings: Settings, icon=None) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("Dynamic Island — настройки")
        if icon is not None:
            self.setWindowIcon(icon)
        self.setMinimumWidth(500)
        self._settings = settings

        tabs = QTabWidget()
        tabs.addTab(self._build_general(), "Общие")
        tabs.addTab(self._build_appearance(), "Вид")
        tabs.addTab(self._build_games(), "Игры")
        tabs.addTab(self._build_spotify(), "Spotify")

        self.save_button = QPushButton("Сохранить")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._apply)
        close_button = QPushButton("Закрыть")
        close_button.clicked.connect(self.close)
        self.saved_label = QLabel("")
        self.saved_label.setStyleSheet("color: #3a9a5b")

        bottom = QHBoxLayout()
        bottom.addWidget(self.saved_label)
        bottom.addStretch(1)
        bottom.addWidget(self.save_button)
        bottom.addWidget(close_button)

        root = QVBoxLayout(self)
        root.addWidget(tabs)
        root.addLayout(bottom)
        self.load(settings)

    # ------------------------------------------------------------------ tabs
    def _build_general(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.autostart = QCheckBox("Запускать вместе с Windows")
        self.monitor = QComboBox()
        form.addRow(self.autostart)
        form.addRow("Монитор:", self.monitor)

        self.overlay_button = QPushButton("Скрыть Dynamic Island")
        self.overlay_button.clicked.connect(self.toggle_overlay)
        quit_button = QPushButton("Завершить приложение")
        quit_button.clicked.connect(self.quit_requested)
        row = QHBoxLayout()
        row.addWidget(self.overlay_button)
        row.addWidget(quit_button)
        form.addRow(row)
        return page

    def _build_appearance(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.compact_width = _spin("compact_width")
        self.expanded_width = _spin("expanded_width")
        self.height_box = _spin("height", step=2)
        self.collapse_delay = _spin("collapse_delay_ms", " мс", 50)
        self.show_cover = QCheckBox("Показывать обложку")
        self.show_progress = QCheckBox("Показывать прогресс трека")
        self.equalizer = QCheckBox("Анимация эквалайзера")
        form.addRow("Ширина (компактно):", self.compact_width)
        form.addRow("Ширина (раскрыто):", self.expanded_width)
        form.addRow("Высота капсулы:", self.height_box)
        form.addRow("Задержка сворачивания:", self.collapse_delay)
        form.addRow(self.show_cover)
        form.addRow(self.show_progress)
        form.addRow(self.equalizer)
        return page

    def _build_games(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.hide_in_games = QCheckBox("Скрывать во время игр")
        self.hide_in_fullscreen = QCheckBox("Скрывать в полноэкранных приложениях")
        self.game_mode = QCheckBox("Игровой режим вручную (скрыть до отключения)")
        for box in (self.hide_in_games, self.hide_in_fullscreen, self.game_mode):
            layout.addWidget(box)

        lists = QHBoxLayout()
        self.excluded = QPlainTextEdit()
        self.excluded.setPlaceholderText("vlc.exe\nmpc-hc64.exe")
        self.games = QPlainTextEdit()
        self.games.setPlaceholderText("game.exe")
        for title, edit in (("Никогда не скрывать для:", self.excluded), ("Всегда считать игрой:", self.games)):
            box = QGroupBox(title)
            inner = QVBoxLayout(box)
            inner.addWidget(edit)
            lists.addWidget(box)
        layout.addLayout(lists)
        hint = QLabel("Имена процессов, по одному в строке (например, game.exe).")
        hint.setStyleSheet("color: gray")
        layout.addWidget(hint)

        self.detector_status = QLabel("—")
        self.detector_status.setWordWrap(True)
        self.detector_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        diag = QGroupBox("Сейчас")
        QVBoxLayout(diag).addWidget(self.detector_status)
        layout.addWidget(diag)
        return page

    def _build_spotify(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.source_label = QLabel("Источник данных: —")
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)

        info = QLabel(
            "Основной источник — системные медиа-элементы Windows (SMTC): название, исполнитель, обложка, "
            "прогресс и кнопки работают без настройки.<br><br>"
            "Web API нужен только как запасной источник (например, Spotify играет на телефоне). "
            "Создайте приложение на <a href='https://developer.spotify.com/dashboard'>developer.spotify.com/dashboard</a>, "
            "добавьте указанный ниже Redirect URI и вставьте Client ID. Секрет не нужен (PKCE). "
            "Управление воспроизведением через Web API требует Spotify Premium.")
        info.setWordWrap(True)
        info.setOpenExternalLinks(True)
        info.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(info)

        form = QFormLayout()
        self.client_id = QLineEdit()
        self.client_id.setPlaceholderText("32 символа из Spotify Dashboard")
        self.port = _spin("spotify_redirect_port", "", 1)
        self.redirect_uri = QLineEdit()
        self.redirect_uri.setReadOnly(True)
        self.port.valueChanged.connect(lambda v: self.redirect_uri.setText(redirect_uri_for(v)))
        copy = QPushButton("Копировать")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.redirect_uri.text()))
        uri_row = QHBoxLayout()
        uri_row.addWidget(self.redirect_uri)
        uri_row.addWidget(copy)
        form.addRow("Client ID:", self.client_id)
        form.addRow("Порт:", self.port)
        form.addRow("Redirect URI:", uri_row)
        layout.addLayout(form)

        self.spotify_status = QLabel("Web API: —")
        self.spotify_status.setWordWrap(True)
        layout.addWidget(self.spotify_status)

        buttons = QHBoxLayout()
        for text, signal in (("Подключить", self.spotify_connect), ("Проверить", self.spotify_check),
                             ("Переподключить", self.spotify_reconnect), ("Отключить", self.spotify_disconnect)):
            button = QPushButton(text)
            if signal in (self.spotify_connect, self.spotify_reconnect):
                button.clicked.connect(self._apply)         # save the Client ID first
            button.clicked.connect(signal)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ data
    def _fill_monitors(self, selected: str) -> None:
        self.monitor.clear()
        self.monitor.addItem("Основной монитор", "")
        for screen in QGuiApplication.screens():
            geo = screen.geometry()
            ratio = screen.devicePixelRatio()
            label = f"{screen.name()} — {round(geo.width() * ratio)}×{round(geo.height() * ratio)}"
            self.monitor.addItem(label, screen.name())
        index = self.monitor.findData(selected)
        self.monitor.setCurrentIndex(max(0, index))

    def load(self, s: Settings) -> None:
        self._settings = s
        self.autostart.setChecked(s.autostart)
        self._fill_monitors(s.monitor)
        self.compact_width.setValue(s.compact_width)
        self.expanded_width.setValue(s.expanded_width)
        self.height_box.setValue(s.height)
        self.collapse_delay.setValue(s.collapse_delay_ms)
        self.show_cover.setChecked(s.show_cover)
        self.show_progress.setChecked(s.show_progress)
        self.equalizer.setChecked(s.equalizer)
        self.hide_in_games.setChecked(s.hide_in_games)
        self.hide_in_fullscreen.setChecked(s.hide_in_fullscreen)
        self.game_mode.setChecked(s.game_mode_manual)
        self.excluded.setPlainText("\n".join(s.excluded_processes))
        self.games.setPlainText("\n".join(s.game_processes))
        self.client_id.setText(s.spotify_client_id)
        self.port.setValue(s.spotify_redirect_port)
        self.redirect_uri.setText(redirect_uri_for(s.spotify_redirect_port))
        self.set_overlay_visible(s.overlay_visible)

    def collect(self) -> Settings:
        return replace(
            self._settings,
            autostart=self.autostart.isChecked(),
            monitor=self.monitor.currentData() or "",
            compact_width=self.compact_width.value(),
            expanded_width=self.expanded_width.value(),
            height=self.height_box.value(),
            collapse_delay_ms=self.collapse_delay.value(),
            show_cover=self.show_cover.isChecked(),
            show_progress=self.show_progress.isChecked(),
            equalizer=self.equalizer.isChecked(),
            hide_in_games=self.hide_in_games.isChecked(),
            hide_in_fullscreen=self.hide_in_fullscreen.isChecked(),
            game_mode_manual=self.game_mode.isChecked(),
            excluded_processes=self.excluded.toPlainText().splitlines(),
            game_processes=self.games.toPlainText().splitlines(),
            spotify_client_id=self.client_id.text(),
            spotify_redirect_port=self.port.value(),
        ).validated()

    def _apply(self) -> None:
        settings = self.collect()
        self.settings_applied.emit(settings)
        self.saved_label.setText("Сохранено")

    # ------------------------------------------------------------------ status updates from the app
    def set_overlay_visible(self, visible: bool) -> None:
        self.overlay_button.setText("Скрыть Dynamic Island" if visible else "Показать Dynamic Island")

    def set_spotify_status(self, status: SpotifyStatus, message: str) -> None:
        text = f"Web API: {_STATUS_TEXT.get(status, status.value)}"
        if message:
            text += f" — {message}"
        self.spotify_status.setText(text)

    def set_source(self, text: str) -> None:
        self.source_label.setText(f"Источник данных: {text}")

    def set_detector_status(self, text: str) -> None:
        self.detector_status.setText(text)

    def present(self) -> None:
        self._fill_monitors(self._settings.monitor)
        self.saved_label.setText("")
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
