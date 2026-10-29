"""The overlay window: frameless, translucent, top-most, no taskbar entry, never takes focus.

Click-through: the window is WS_EX_TRANSPARENT (all clicks fall through to windows below) until
the cursor is over the capsule; a light adaptive cursor poll (no global hooks) toggles it.
"""
from __future__ import annotations

import logging
import time

import win32con
import win32gui
from PySide6.QtCore import QEasingCurve, QPointF, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QMouseEvent, QPainter, QScreen, QWheelEvent
from PySide6.QtWidgets import QWidget

from app.animations import HoverIntent
from app.config import Settings
from app.island_widget import IslandView
from app.media.models import Command, MediaState

log = logging.getLogger(__name__)

POLL_NEAR_MS = 30
POLL_FAR_MS = 150
NEAR_DISTANCE = 120
SWP_FLAGS = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | win32con.SWP_NOOWNERZORDER


class IslandWindow(QWidget):
    command_requested = Signal(object)          # Command
    seek_requested = Signal(int)                # position, ms
    volume_requested = Signal(float)            # Spotify volume 0..1
    repeat_requested = Signal(object)           # RepeatMode

    def __init__(self, settings: Settings) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setObjectName("DynamicIsland")
        self.setWindowTitle("Dynamic Island")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.settings = settings
        self.view = IslandView(settings, self)
        self.view.update_requested.connect(self.update)

        self.hover = HoverIntent(settings.collapse_delay_ms, self)
        self.hover.expand_requested.connect(self._on_expand)
        self.hover.collapse_requested.connect(lambda: self.view.set_expanded(False))

        self.volume_provider = None                 # callable -> float | None, set by the application
        self._last_volume_emit = 0.0
        self._interactive = False
        self._wanted_visible = False
        self._screen: QScreen | None = None

        self._pointer_timer = QTimer(self)
        self._pointer_timer.setInterval(POLL_FAR_MS)
        self._pointer_timer.timeout.connect(self._poll_pointer)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(220)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._on_fade_finished)

        app = QGuiApplication.instance()
        app.screenAdded.connect(self._on_screens_changed)
        app.screenRemoved.connect(self._on_screens_changed)
        app.primaryScreenChanged.connect(self._on_screens_changed)
        self.relayout()

    # ------------------------------------------------------------------ public API
    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.hover.set_delay(settings.collapse_delay_ms)
        self.view.apply_settings(settings)
        self.relayout()

    def set_media_state(self, state: MediaState) -> None:
        self.view.set_state(state)

    def show_notice(self, text: str) -> None:
        self.view.show_notice(text)

    @property
    def wanted_visible(self) -> bool:
        return self._wanted_visible

    def set_overlay_visible(self, visible: bool, animated: bool = True) -> None:
        if visible == self._wanted_visible and (visible == self.isVisible()):
            return
        self._wanted_visible = visible
        self._fade.stop()
        if visible:
            self.relayout()
            if animated:
                self.setWindowOpacity(0.0)
            self.show()
            self._apply_win_styles()
            self._raise_topmost()
            self.view.set_active(True)
            self._pointer_timer.start(POLL_FAR_MS)
            if animated:
                self._fade.setStartValue(0.0)
                self._fade.setEndValue(1.0)
                self._fade.start()
            else:
                self.setWindowOpacity(1.0)
        else:
            # hide immediately (a game is taking over the screen) - no fade, no input capture
            self._pointer_timer.stop()
            self.hover.force_collapse()
            self.view.expansion.jump(0.0)
            self.view.set_active(False)
            self._set_interactive(False)
            self.hide()

    def shutdown(self) -> None:
        self._pointer_timer.stop()
        self._fade.stop()
        self.view.stop()
        self.hide()

    @property
    def pointer_polling(self) -> bool:
        return self._pointer_timer.isActive()

    # ------------------------------------------------------------------ positioning
    def target_screen(self) -> QScreen:
        name = self.settings.monitor
        if name:
            for screen in QGuiApplication.screens():
                if screen.name() == name:
                    return screen
        return QGuiApplication.primaryScreen()

    def relayout(self) -> None:
        screen = self.target_screen()
        if screen is None:
            return
        if screen is not self._screen:
            if self._screen is not None:
                try:
                    self._screen.geometryChanged.disconnect(self._on_geometry_changed)
                except (RuntimeError, TypeError):
                    pass
            self._screen = screen
            screen.geometryChanged.connect(self._on_geometry_changed)
        size = self.view.window_size()
        geo = screen.geometry()
        x = geo.x() + (geo.width() - size.width()) // 2
        y = geo.y()
        if self.size() != size:
            self.setFixedSize(size)
        if self.pos().x() != x or self.pos().y() != y:
            self.move(x, y)
        if self.windowHandle() is not None and self.windowHandle().screen() is not screen:
            self.windowHandle().setScreen(screen)
            self.move(x, y)

    def _on_geometry_changed(self, *_args) -> None:
        log.info("Screen geometry changed; repositioning")
        self.relayout()

    def _on_screens_changed(self, *_args) -> None:
        self._screen = None if self._screen not in QGuiApplication.screens() else self._screen
        QTimer.singleShot(300, self.relayout)       # let Windows settle the new layout first

    # ------------------------------------------------------------------ Win32 styles
    def _hwnd(self) -> int:
        return int(self.winId())

    def _apply_win_styles(self) -> None:
        try:
            hwnd = self._hwnd()
            ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            want = ex | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE
            if ex & win32con.WS_EX_LAYERED:          # click-through only works on layered windows
                if self._interactive:
                    want &= ~win32con.WS_EX_TRANSPARENT
                else:
                    want |= win32con.WS_EX_TRANSPARENT
            if want != ex:
                win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, want)
        except Exception as exc:
            log.debug("Cannot update window styles: %s", exc)

    def _set_interactive(self, interactive: bool) -> None:
        if interactive == self._interactive:
            return
        self._interactive = interactive
        self._apply_win_styles()

    def _raise_topmost(self) -> None:
        try:
            win32gui.SetWindowPos(self._hwnd(), win32con.HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)
        except Exception as exc:
            log.debug("SetWindowPos failed: %s", exc)

    @property
    def interactive(self) -> bool:
        return self._interactive

    # ------------------------------------------------------------------ hover
    def _poll_pointer(self) -> None:
        global_pos = QCursor.pos()
        local = QPointF(self.mapFromGlobal(global_pos))
        inside = self.view.hit_rect(self.width()).contains(local)
        self.pointer_moved(inside)
        near = self.frameGeometry().adjusted(-NEAR_DISTANCE, -NEAR_DISTANCE,
                                             NEAR_DISTANCE, NEAR_DISTANCE).contains(global_pos)
        interval = POLL_NEAR_MS if (near or self.hover.expanded) else POLL_FAR_MS
        if self._pointer_timer.interval() != interval:
            self._pointer_timer.setInterval(interval)

    def pointer_moved(self, inside: bool) -> None:
        """Feed pointer presence (separate from polling so it can be driven in tests)."""
        if self.view.dragging:                      # keep the island open while a slider is held
            inside = True
        if inside and not self.hover.hovered:
            self._set_interactive(True)
            self.hover.pointer_entered()
        elif not inside and self.hover.hovered:
            self._set_interactive(False)
            self.view.hover_button = self.view.pressed_button = None
            self.hover.pointer_left()
            self.update()

    def _on_expand(self) -> None:
        self._raise_topmost()
        self.refresh_volume()
        self.view.set_expanded(True)

    # ------------------------------------------------------------------ seek / volume
    def refresh_volume(self) -> None:
        level = None
        if self.volume_provider is not None:
            try:
                level = self.volume_provider()
            except Exception as exc:
                log.debug("Volume query failed: %s", exc)
        self.view.set_volume_level(level)

    def _change_volume(self, level: float, final: bool = True) -> None:
        """Update the slider immediately; throttle requests while dragging (matters for the Web API)."""
        level = min(1.0, max(0.0, level))
        self.view.set_volume_level(level)
        self.view.touch_volume()
        now = time.monotonic()
        if final or now - self._last_volume_emit >= 0.08:
            self._last_volume_emit = now
            self.volume_requested.emit(level)

    def _drag_to(self, pos: QPointF, final: bool) -> None:
        frac = self.view.bar_fraction(pos, self.width())
        if self.view.dragging == "volume":
            self._change_volume(frac, final)
        elif self.view.dragging == "seek":
            self.view.scrub_ms = int(frac * (self.view.state.duration_ms or 0))
            self.update()

    def _press_row(self, target: str, pos: QPointF) -> None:
        view = self.view
        if target == "toggle":
            if view.in_volume_mode:
                view.leave_volume_mode()
            else:
                self.refresh_volume()
                view.enter_volume_mode()
        elif target == "repeat":
            mode = view.state.repeat.next()
            view.apply_local_repeat(mode)           # instant feedback; the player confirms shortly
            self.repeat_requested.emit(mode)
        elif target == "mute":
            self._change_volume(view.toggle_mute())
        elif target == "bar":
            view.dragging = "volume" if view.in_volume_mode else "seek"
            self._drag_to(pos, final=False)
        self.update()

    # ------------------------------------------------------------------ Qt events
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        try:
            self.view.paint(painter, self.width())
        finally:
            painter.end()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        if self.view.dragging:
            self._drag_to(pos, final=False)
            return
        button = self.view.button_at(pos, self.width())
        row = self.view.row_target(pos, self.width()) if button is None else None
        if button != self.view.hover_button or row != self.view.row_hover:
            self.view.hover_button, self.view.row_hover = button, row
            clickable = button is not None or row is not None
            self.setCursor(Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor)
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        row = self.view.row_target(event.position(), self.width())
        if row is not None:
            self._press_row(row, event.position())
            return
        self.view.pressed_button = self.view.button_at(event.position(), self.width())
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self.view.dragging:
            self._drag_to(event.position(), final=True)
            if self.view.dragging == "seek" and self.view.scrub_ms is not None:
                self.seek_requested.emit(self.view.scrub_ms)
                self.view.apply_local_seek(self.view.scrub_ms)
            self.view.dragging = self.view.scrub_ms = None
            self.update()
            return
        pressed, self.view.pressed_button = self.view.pressed_button, None
        released = self.view.button_at(event.position(), self.width())
        if pressed is not None and pressed == released and self.view.button_enabled(pressed):
            self.command_requested.emit(pressed)
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Mouse wheel over the open island = Spotify volume (5% per notch)."""
        if self.view.expansion.value < 0.7 or not self.view.state.has_track:
            return
        if self.view.volume is None:
            self.refresh_volume()
            if self.view.volume is None:
                self.view.show_notice("Громкость Spotify пока недоступна")
                return
        steps = event.angleDelta().y() / 120
        if not steps:
            return
        self.view.enter_volume_mode()
        self._change_volume(self.view.volume + 0.05 * steps)
        event.accept()

    def leaveEvent(self, _event) -> None:
        if self.view.row_hover is not None:
            self.view.row_hover = None
            self.update()
        if self.view.hover_button is not None:
            self.view.hover_button = None
            self.unsetCursor()
            self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_win_styles()

    def _on_fade_finished(self) -> None:
        if not self._wanted_visible:
            self.hide()
