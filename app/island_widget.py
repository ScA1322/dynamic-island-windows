"""Visual model and painter of the island.

Everything is drawn with QPainter inside one fixed-size translucent window: the capsule grows and
shrinks inside it, so the OS window is never resized or moved while animating (no flicker/jumps).
"""
from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import replace

from PySide6.QtCore import QElapsedTimer, QEasingCurve, QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QPixmap, QTransform

from app.animations import AnimatedValue, clamp01, ease_curve, lerp, smoothstep
from app.config import Settings
from app.media.models import EMPTY_STATE, Command, MediaState, RepeatMode

TOP_MARGIN = 6          # gap between the screen edge and the capsule
SIDE_MARGIN = 14        # transparent border inside the window
PAD = 16
COVER_E = 58            # expanded cover size
ROW_NO_COVER = 44
PROGRESS_ROW = 26        # 12 px gap + 14 px row
BOTTOM_PAD_PROGRESS = 14
BTN = 34
BTN_PLAY = 38
BTN_GAP = 42
EXPANDED_RADIUS = 30
OVERSHOOT_ROOM = 0.12
ICON = 16               # speaker icon in the progress row
VOLUME_MODE_MS = 3000   # volume row returns to progress after this much inactivity

WHITE = QColor(255, 255, 255)
MUTED = QColor(160, 160, 165)
NOTICE = QColor(255, 179, 64)


def _font(pixel_size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont("Segoe UI Variable Text")
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI", "Arial"])
    font.setPixelSize(pixel_size)
    font.setWeight(weight)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


def format_time(ms: int | None) -> str:
    if ms is None or ms < 0:
        return "–:––"
    total = int(ms // 1000)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def accent_from_image(image: QImage) -> QColor:
    """Average cover colour, lifted so it reads well on black."""
    if image.isNull():
        return QColor(230, 230, 230)
    avg = image.scaled(1, 1, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation).pixelColor(0, 0)
    h, s, v, _ = avg.getHsvF()
    if s < 0.12 or h < 0:
        return QColor(225, 225, 230)
    return QColor.fromHsvF(h, min(1.0, max(s, 0.45)), max(v, 0.88))


def _lerp_rect(a: QRectF, b: QRectF, t: float) -> QRectF:
    return QRectF(lerp(a.x(), b.x(), t), lerp(a.y(), b.y(), t), lerp(a.width(), b.width(), t), lerp(a.height(), b.height(), t))


class IslandView(QObject):
    """Holds animated visual state and paints it. Emits update_requested when a repaint is needed."""

    update_requested = Signal()

    def __init__(self, settings: Settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.cfg = settings
        self.state: MediaState = EMPTY_STATE
        self.prev_state: MediaState | None = None
        self.active = True                      # window visible; timers run only then
        self.hover_button: Command | None = None
        self.pressed_button: Command | None = None
        self.notice = ""
        self.volume: float | None = None        # Spotify volume 0..1; None = cannot be controlled
        self.scrub_ms: int | None = None        # position preview while dragging the progress bar
        self.dragging: str | None = None        # "seek" | "volume"
        self.row_hover: str | None = None       # "bar" | "toggle" | "mute" | "repeat"
        self._pre_mute = 0.5

        self.expansion = AnimatedValue(0.0, 320, ease_curve(QEasingCurve.Type.OutBack, 0.9), self)
        self.swap = AnimatedValue(1.0, 380, QEasingCurve(QEasingCurve.Type.OutCubic), self)
        self.height_anim = AnimatedValue(self.expanded_height_for(EMPTY_STATE), 260,
                                         QEasingCurve(QEasingCurve.Type.OutCubic), self)
        self.eq_level = AnimatedValue(0.0, 300, QEasingCurve(QEasingCurve.Type.OutCubic), self)
        self.vol_mode = AnimatedValue(0.0, 220, QEasingCurve(QEasingCurve.Type.OutCubic), self)
        for anim in (self.expansion, self.swap, self.height_anim, self.eq_level, self.vol_mode):
            anim.changed.connect(self._on_anim)

        self._pixmaps: OrderedDict[str, QPixmap] = OrderedDict()
        self._accents: dict[str, QColor] = {}
        self._clock = QElapsedTimer()
        self._clock.start()

        self._eq_timer = QTimer(self)
        self._eq_timer.setInterval(40)
        self._eq_timer.timeout.connect(self.update_requested)
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(250)
        self._progress_timer.timeout.connect(self.update_requested)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.setInterval(3000)
        self._notice_timer.timeout.connect(self._clear_notice)
        self._volume_timer = QTimer(self)
        self._volume_timer.setSingleShot(True)
        self._volume_timer.setInterval(VOLUME_MODE_MS)
        self._volume_timer.timeout.connect(self._volume_idle)

        self.f_title = _font(14, QFont.Weight.DemiBold)
        self.f_artist = _font(12)
        self.f_time = _font(11)
        self.f_idle = _font(13, QFont.Weight.Medium)

    # ------------------------------------------------------------------ geometry
    def expanded_height_for(self, state: MediaState) -> float:
        if not state.has_track:
            return float(max(self.cfg.height + 20, 56))
        row = COVER_E if self.cfg.show_cover else ROW_NO_COVER
        if self.cfg.show_progress and state.duration_ms:
            return float(PAD + row + PROGRESS_ROW + BOTTOM_PAD_PROGRESS)
        return float(PAD + row + PAD)

    def max_expanded_height(self) -> float:
        return float(PAD + max(COVER_E, ROW_NO_COVER) + PROGRESS_ROW + BOTTOM_PAD_PROGRESS)

    def window_size(self) -> QSize:
        cw, ch, ew = self.cfg.compact_width, self.cfg.height, self.cfg.expanded_width
        eh = self.max_expanded_height()
        w = ew + (ew - cw) * OVERSHOOT_ROOM + 2 * SIDE_MARGIN
        h = TOP_MARGIN + eh + (eh - ch) * OVERSHOOT_ROOM + SIDE_MARGIN
        return QSize(math.ceil(w), math.ceil(h))

    def capsule_rect(self, widget_width: float) -> QRectF:
        t = self.expansion.value
        w = lerp(self.cfg.compact_width, self.cfg.expanded_width, t)
        h = lerp(self.cfg.height, self.height_anim.value, t)
        return QRectF((widget_width - w) / 2, TOP_MARGIN, w, h)

    def hit_rect(self, widget_width: float) -> QRectF:
        """Hover zone: the capsule plus the strip above it up to the screen edge."""
        r = self.capsule_rect(widget_width)
        return QRectF(r.left() - 4, 0, r.width() + 8, r.bottom() + 4)

    def _radius(self, rect: QRectF) -> float:
        t = clamp01(self.expansion.value)
        return min(rect.height() / 2, lerp(self.cfg.height / 2, EXPANDED_RADIUS, t))

    def _compact_cover_rect(self, r: QRectF) -> QRectF:
        ch = self.cfg.height
        size = ch - 12
        inset = (ch - size) / 2
        return QRectF(r.left() + inset + 3, r.top() + inset, size, size)

    def _expanded_cover_rect(self, r: QRectF) -> QRectF:
        return QRectF(r.left() + PAD, r.top() + PAD, COVER_E, COVER_E)

    def _row_center_y(self, r: QRectF) -> float:
        row = COVER_E if self.cfg.show_cover else ROW_NO_COVER
        return r.top() + PAD + row / 2

    def button_rects(self, r: QRectF) -> dict[Command, QRectF]:
        cy = self._row_center_y(r)
        next_cx = r.right() - PAD - BTN / 2
        play_cx = next_cx - BTN_GAP
        prev_cx = play_cx - BTN_GAP
        return {
            Command.PREVIOUS: QRectF(prev_cx - BTN / 2, cy - BTN / 2, BTN, BTN),
            Command.TOGGLE: QRectF(play_cx - BTN_PLAY / 2, cy - BTN_PLAY / 2, BTN_PLAY, BTN_PLAY),
            Command.NEXT: QRectF(next_cx - BTN / 2, cy - BTN / 2, BTN, BTN),
        }

    def button_enabled(self, command: Command) -> bool:
        return self.state.has_track and self.state.caps.allows(command)

    def button_at(self, pos: QPointF, widget_width: float) -> Command | None:
        if self.expansion.value < 0.7 or not self.state.has_track:
            return None
        for command, rect in self.button_rects(self.capsule_rect(widget_width)).items():
            if rect.adjusted(-3, -3, 3, 3).contains(pos):
                return command
        return None

    def progress_layout(self, r: QRectF) -> dict[str, QRectF | None]:
        row = COVER_E if self.cfg.show_cover else ROW_NO_COVER
        y = r.top() + PAD + row + 12 + 7
        label_w = 46.0 if (self.state.duration_ms or 0) >= 3600_000 else 38.0
        right_edge = r.right() - PAD
        toggle = repeat = None
        if self.volume is not None:
            toggle = QRectF(right_edge - ICON, y - ICON / 2, ICON, ICON)
            right_edge = toggle.left() - 10
        if self.repeat_visible:
            repeat = QRectF(right_edge - ICON, y - ICON / 2, ICON, ICON)
            right_edge = repeat.left() - 8
        left = QRectF(r.left() + PAD, y - 7, label_w, 14)
        right = QRectF(right_edge - label_w, y - 7, label_w, 14)
        bar = QRectF(left.right() + 6, y - 2, right.left() - left.right() - 12, 4)
        return {"left": left, "bar": bar, "right": right, "toggle": toggle, "repeat": repeat}

    @property
    def repeat_visible(self) -> bool:
        return self.state.repeat is not None and self.state.caps.repeat

    def apply_local_repeat(self, mode: RepeatMode) -> None:
        self.state = replace(self.state, repeat=mode)
        self.update_requested.emit()

    @property
    def row_visible(self) -> bool:
        return self.state.has_track and self.cfg.show_progress and bool(self.state.duration_ms)

    @property
    def in_volume_mode(self) -> bool:
        return self.vol_mode.target > 0.5

    def row_target(self, pos: QPointF, widget_width: float) -> str | None:
        """What in the progress row is under pos: "bar", "toggle", "mute" or None."""
        if self.expansion.value < 0.7 or not self.row_visible:
            return None
        lay = self.progress_layout(self.capsule_rect(widget_width))
        if lay["toggle"] is not None and lay["toggle"].adjusted(-5, -5, 4, 5).contains(pos):
            return "toggle"
        if lay["repeat"] is not None and lay["repeat"].adjusted(-4, -5, 4, 5).contains(pos):
            return "repeat"
        if self.in_volume_mode and lay["left"].adjusted(-2, -6, 2, 6).contains(pos):
            return "mute"
        if lay["bar"].adjusted(-4, -9, 4, 9).contains(pos):
            if self.in_volume_mode or self.state.caps.seek:
                return "bar"
        return None

    def bar_fraction(self, pos: QPointF, widget_width: float) -> float:
        bar = self.progress_layout(self.capsule_rect(widget_width))["bar"]
        return clamp01((pos.x() - bar.left()) / bar.width()) if bar.width() > 0 else 0.0

    # ------------------------------------------------------------------ seek / volume
    def apply_local_seek(self, position_ms: int) -> None:
        """Show the new position right away; the player's next update confirms or corrects it."""
        self.state = replace(self.state, position_ms=int(position_ms), position_timestamp=time.monotonic())
        self.update_requested.emit()

    def set_volume_level(self, level: float | None) -> None:
        self.volume = None if level is None else clamp01(level)
        if self.volume is None and self.in_volume_mode:
            self.vol_mode.animate_to(0.0)
        self.update_requested.emit()

    def enter_volume_mode(self) -> None:
        if self.volume is None:
            return
        self.vol_mode.animate_to(1.0)
        self._volume_timer.start()

    def leave_volume_mode(self) -> None:
        self._volume_timer.stop()
        self.vol_mode.animate_to(0.0)

    def touch_volume(self) -> None:
        self._volume_timer.start()

    def toggle_mute(self) -> float:
        """Return the new level: 0 if it was audible, otherwise the level before muting."""
        if self.volume and self.volume > 0.001:
            self._pre_mute = self.volume
            return 0.0
        return self._pre_mute or 0.5

    def _volume_idle(self) -> None:
        if self.dragging or self.row_hover:
            self._volume_timer.start()
        else:
            self.leave_volume_mode()

    # ------------------------------------------------------------------ state
    def apply_settings(self, settings: Settings) -> None:
        self.cfg = settings
        self.height_anim.jump(self.expanded_height_for(self.state))
        self._update_timers()
        self.update_requested.emit()

    def set_state(self, state: MediaState) -> None:
        old = self.state
        if state.track_key != old.track_key or state.cover_path != old.cover_path:
            self.prev_state = old
            self.swap.jump(0.0)
            self.swap.animate_to(1.0)
        self.state = state
        if state.cover_path:
            self._pixmap(state.cover_path)
        self.eq_level.animate_to(1.0 if state.is_playing else 0.0)
        target_h = self.expanded_height_for(state)
        if self.expansion.value < 0.01:
            self.height_anim.jump(target_h)
        else:
            self.height_anim.animate_to(target_h, span=60)
        self._update_timers()
        self.update_requested.emit()

    def set_expanded(self, expanded: bool) -> None:
        if expanded:
            self.expansion.animate_to(1.0, 320, ease_curve(QEasingCurve.Type.OutBack, 0.9))
        else:
            self.expansion.animate_to(0.0, 260, QEasingCurve(QEasingCurve.Type.InOutCubic))
            self.leave_volume_mode()
            self.scrub_ms = self.dragging = self.row_hover = None
        self._update_timers()

    def set_active(self, active: bool) -> None:
        self.active = active
        self._update_timers()

    def show_notice(self, text: str) -> None:
        self.notice = text
        self._notice_timer.start()
        self.update_requested.emit()

    def _clear_notice(self) -> None:
        self.notice = ""
        self.update_requested.emit()

    def _on_anim(self, _value: float) -> None:
        if self.prev_state is not None and self.swap.value >= 1.0:
            self.prev_state = None          # release the previous cover
        self._update_timers()
        self.update_requested.emit()

    def _update_timers(self) -> None:
        compact_visible = self.expansion.value < 0.45
        eq_on = (self.active and self.cfg.equalizer and self.state.has_track and compact_visible
                 and (self.state.is_playing or self.eq_level.running))
        if eq_on != self._eq_timer.isActive():
            self._eq_timer.start() if eq_on else self._eq_timer.stop()
        progress_on = (self.active and self.cfg.show_progress and self.state.is_playing
                       and bool(self.state.duration_ms) and self.expansion.target > 0.5)
        if progress_on != self._progress_timer.isActive():
            self._progress_timer.start() if progress_on else self._progress_timer.stop()

    @property
    def timers_running(self) -> bool:
        return self._eq_timer.isActive() or self._progress_timer.isActive()

    def stop(self) -> None:
        for timer in (self._eq_timer, self._progress_timer, self._notice_timer, self._volume_timer):
            timer.stop()
        for anim in (self.expansion, self.swap, self.height_anim, self.eq_level, self.vol_mode):
            anim.stop()

    # ------------------------------------------------------------------ pixmaps
    def _pixmap(self, path: str | None) -> QPixmap | None:
        if not path:
            return None
        pm = self._pixmaps.get(path)
        if pm is not None:
            self._pixmaps.move_to_end(path)
            return pm
        image = QImage(path)
        if image.isNull():
            return None
        if image.width() > 256 or image.height() > 256:
            image = image.scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                 Qt.TransformationMode.SmoothTransformation)
        self._accents[path] = accent_from_image(image)
        pm = QPixmap.fromImage(image)
        self._pixmaps[path] = pm
        while len(self._pixmaps) > 4:
            old, _ = self._pixmaps.popitem(last=False)
            self._accents.pop(old, None)
        return pm

    def accent(self) -> QColor:
        return QColor(self._accents.get(self.state.cover_path or "", QColor(235, 235, 240)))

    @property
    def cached_pixmaps(self) -> int:
        return len(self._pixmaps)

    # ------------------------------------------------------------------ painting
    def paint(self, p: QPainter, widget_width: float) -> None:
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
                         | QPainter.RenderHint.TextAntialiasing)
        r = self.capsule_rect(widget_width)
        radius = self._radius(r)
        t = self.expansion.value
        alpha_c = 1.0 - smoothstep(0.0, 0.45, t)
        alpha_e = smoothstep(0.35, 1.0, t)

        p.setPen(QPen(QColor(255, 255, 255, 18), 1))
        p.setBrush(QColor(0, 0, 0))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        p.save()
        p.setClipRect(r.adjusted(1, 1, -1, -1))
        s = self.swap.value
        layers = [(self.prev_state, 1.0 - s, -1)] if self.prev_state is not None and s < 1.0 else []
        layers.append((self.state, s if layers else 1.0, 1 if layers else 0))

        for state, alpha, direction in layers:
            if alpha <= 0.01:
                continue
            self._paint_cover(p, r, state, alpha)
            if alpha_e > 0.01:
                dy = 5 * (1 - alpha) * direction
                self._paint_text(p, r, state, alpha * alpha_e, dy)

        if alpha_c > 0.01 and self.cfg.equalizer and self.state.has_track:
            self._paint_equalizer(p, r, alpha_c)
        if alpha_e > 0.01 and self.state.has_track:
            self._paint_buttons(p, r, alpha_e)
            if self.row_visible:
                vm = self.vol_mode.value
                if vm < 0.99:
                    self._paint_progress(p, r, alpha_e * (1 - vm))
                if vm > 0.01:
                    self._paint_volume(p, r, alpha_e * vm)
                self._paint_volume_toggle(p, r, alpha_e)
                self._paint_repeat(p, r, alpha_e)
        p.restore()

    def _paint_cover(self, p: QPainter, r: QRectF, state: MediaState, alpha: float) -> None:
        if not self.cfg.show_cover or not state.has_track:
            return
        te = smoothstep(0.0, 1.0, clamp01(self.expansion.value))
        rect = _lerp_rect(self._compact_cover_rect(r), self._expanded_cover_rect(r), te)
        radius = lerp(6, 13, te)
        pm = self._pixmap(state.cover_path)
        p.save()
        p.setOpacity(alpha)
        p.setPen(Qt.PenStyle.NoPen)
        if pm is None or pm.isNull():
            p.setBrush(QColor(42, 42, 46))
            p.drawRoundedRect(rect, radius, radius)
            p.setPen(QColor(150, 150, 155))
            p.setFont(_font(max(9, int(rect.height() * 0.5))))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "♪")
        else:
            side = min(pm.width(), pm.height())
            brush = QBrush(pm)
            transform = QTransform()
            transform.translate(rect.x(), rect.y())
            transform.scale(rect.width() / side, rect.height() / side)
            transform.translate(-(pm.width() - side) / 2, -(pm.height() - side) / 2)
            brush.setTransform(transform)
            p.setBrush(brush)
            p.drawRoundedRect(rect, radius, radius)
        p.restore()

    def _text_left(self, r: QRectF) -> float:
        return (r.left() + PAD + COVER_E + 14) if self.cfg.show_cover else (r.left() + PAD + 6)

    def _paint_text(self, p: QPainter, r: QRectF, state: MediaState, alpha: float, dy: float) -> None:
        p.save()
        p.setOpacity(alpha)
        if not state.has_track:
            text = self.notice or ("Ничего не воспроизводится" if state.player_available else "Spotify не запущен")
            p.setFont(self.f_idle)
            p.setPen(NOTICE if self.notice else MUTED)
            p.drawText(r.adjusted(PAD, dy, -PAD, dy), Qt.AlignmentFlag.AlignCenter, f"♪  {text}")
            p.restore()
            return
        left = self._text_left(r)
        right = self.button_rects(r)[Command.PREVIOUS].left() - 10
        width = max(10.0, right - left)
        cy = self._row_center_y(r)
        title = QFontMetrics(self.f_title).elidedText(state.title, Qt.TextElideMode.ElideRight, int(width))
        p.setFont(self.f_title)
        p.setPen(WHITE)
        p.drawText(QRectF(left, cy - 21 + dy, width, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        is_current = state is self.state
        subtitle, color = (self.notice, NOTICE) if (self.notice and is_current) else (state.artist, MUTED)
        subtitle = QFontMetrics(self.f_artist).elidedText(subtitle, Qt.TextElideMode.ElideRight, int(width))
        p.setFont(self.f_artist)
        p.setPen(color)
        p.drawText(QRectF(left, cy + 1 + dy, width, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, subtitle)
        p.restore()

    def _paint_equalizer(self, p: QPainter, r: QRectF, alpha: float) -> None:
        ch = self.cfg.height
        bars, bar_w, gap = 4, 3.0, 2.5
        total = bars * bar_w + (bars - 1) * gap
        inset = (ch - (ch - 12)) / 2 + 5
        x0 = r.right() - inset - total
        cy = r.top() + ch / 2
        max_h, min_h = ch * 0.46, 3.0
        level = self.eq_level.value
        seconds = self._clock.elapsed() / 1000.0
        color = self.accent()
        p.save()
        p.setOpacity(alpha)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        for i in range(bars):
            wave = 0.5 + 0.28 * math.sin(seconds * (5.1 + i * 1.7) + i * 1.3) + 0.22 * math.sin(seconds * (8.3 - i) + i * 2.1)
            h = min_h + (max_h - min_h) * clamp01(wave) * level
            p.drawRoundedRect(QRectF(x0 + i * (bar_w + gap), cy - h / 2, bar_w, h), bar_w / 2, bar_w / 2)
        p.restore()

    def _paint_buttons(self, p: QPainter, r: QRectF, alpha: float) -> None:
        for command, rect in self.button_rects(r).items():
            enabled = self.button_enabled(command)
            p.save()
            p.setOpacity(alpha * (1.0 if enabled else 0.3))
            if enabled and command is self.hover_button:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 46 if command is self.pressed_button else 26))
                p.drawEllipse(rect)
            scale = 0.9 if (enabled and command is self.pressed_button) else 1.0
            c = rect.center()
            p.translate(c)
            p.scale(scale, scale)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(WHITE)
            if command is Command.TOGGLE:
                self._draw_play_pause(p, pause=self.state.is_playing)
            else:
                self._draw_skip(p, forward=command is Command.NEXT)
            p.restore()

    @staticmethod
    def _triangle(p: QPainter, x: float, w: float, h: float, forward: bool) -> None:
        path = QPainterPath()
        if forward:
            path.moveTo(x, -h / 2)
            path.lineTo(x + w, 0)
            path.lineTo(x, h / 2)
        else:
            path.moveTo(x + w, -h / 2)
            path.lineTo(x, 0)
            path.lineTo(x + w, h / 2)
        path.closeSubpath()
        p.drawPath(path)

    def _draw_skip(self, p: QPainter, forward: bool) -> None:
        w, h = 8.0, 11.0
        self._triangle(p, -w, w, h, forward)
        self._triangle(p, 0, w, h, forward)

    def _draw_play_pause(self, p: QPainter, pause: bool) -> None:
        if pause:
            bw, bh, gap = 4.2, 15.0, 4.4
            p.drawRoundedRect(QRectF(-gap / 2 - bw, -bh / 2, bw, bh), 1.4, 1.4)
            p.drawRoundedRect(QRectF(gap / 2, -bh / 2, bw, bh), 1.4, 1.4)
        else:
            self._triangle(p, -5.5, 13.5, 16.0, True)

    def _paint_progress(self, p: QPainter, r: QRectF, alpha: float) -> None:
        state = self.state
        duration = state.duration_ms or 0
        position = self.scrub_ms if self.scrub_ms is not None else (state.position_at() or 0)
        frac = clamp01(position / duration) if duration else 0.0
        lay = self.progress_layout(r)
        active = self.dragging == "seek" or (self.row_hover == "bar" and state.caps.seek)
        p.save()
        p.setOpacity(alpha)
        p.setFont(self.f_time)
        p.setPen(WHITE if self.dragging == "seek" else MUTED)
        p.drawText(lay["left"], Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, format_time(position))
        p.setPen(MUTED)
        p.drawText(lay["right"], Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, format_time(duration))
        self._paint_bar(p, lay["bar"], frac, self.accent(), active)
        p.restore()

    def _paint_volume(self, p: QPainter, r: QRectF, alpha: float) -> None:
        level = self.volume or 0.0
        lay = self.progress_layout(r)
        p.save()
        p.setOpacity(alpha)
        icon = QRectF(0, 0, ICON, ICON)
        icon.moveCenter(lay["left"].center())
        self._draw_speaker(p, icon, level, WHITE if self.row_hover == "mute" else MUTED)
        p.setFont(self.f_time)
        p.setPen(MUTED)
        p.drawText(lay["right"], Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{round(level * 100)}%")
        active = self.dragging == "volume" or self.row_hover == "bar"
        self._paint_bar(p, lay["bar"], level, QColor(235, 235, 240), active)
        p.restore()

    def _paint_volume_toggle(self, p: QPainter, r: QRectF, alpha: float) -> None:
        toggle = self.progress_layout(r)["toggle"]
        if toggle is None:
            return
        if self.row_hover == "toggle":
            color = WHITE
        elif self.in_volume_mode:
            color = self.accent()
        else:
            color = MUTED
        p.save()
        p.setOpacity(alpha)
        self._draw_speaker(p, toggle, self.volume or 0.0, color)
        p.restore()

    def _paint_repeat(self, p: QPainter, r: QRectF, alpha: float) -> None:
        rect = self.progress_layout(r)["repeat"]
        if rect is None:
            return
        mode = self.state.repeat
        if self.row_hover == "repeat":
            color = WHITE
        elif mode is RepeatMode.OFF:
            color = MUTED
        else:
            color = self.accent()
        p.save()
        p.setOpacity(alpha)
        self._draw_repeat(p, rect, color, one=mode is RepeatMode.TRACK)
        p.restore()

    @staticmethod
    def _draw_repeat(p: QPainter, rect: QRectF, color: QColor, one: bool) -> None:
        """Loop of two half-rectangles with arrowheads; a small '1' inside for repeat-one."""
        c = rect.center()
        w, h, rad = 13.0, 9.0, 3.0
        pen = QPen(color, 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.save()
        p.translate(c)
        for _ in range(2):                          # second half = first half rotated by 180 degrees
            left, top, right = -w / 2, -h / 2, w / 2
            path = QPainterPath()
            path.moveTo(left, h / 2 - rad)
            path.lineTo(left, top + rad)
            path.arcTo(QRectF(left, top, 2 * rad, 2 * rad), 180, -90)
            path.lineTo(right - 4, top)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            head = QPainterPath()
            head.moveTo(right - 4.5, top - 2.8)
            head.lineTo(right - 0.5, top)
            head.lineTo(right - 4.5, top + 2.8)
            head.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawPath(head)
            p.rotate(180)
        if one:
            font = _font(8, QFont.Weight.Bold)
            p.setFont(font)
            p.setPen(color)
            p.drawText(QRectF(-4, -5, 8, 10), Qt.AlignmentFlag.AlignCenter, "1")
        p.restore()

    @staticmethod
    def _paint_bar(p: QPainter, bar: QRectF, frac: float, color: QColor, active: bool) -> None:
        if active:                                   # a little thicker under the cursor
            bar = bar.adjusted(0, -1, 0, 1)
        radius = bar.height() / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 46))
        p.drawRoundedRect(bar, radius, radius)
        if frac > 0:
            filled = QRectF(bar.left(), bar.top(), max(bar.height(), bar.width() * frac), bar.height())
            p.setBrush(color)
            p.drawRoundedRect(filled, radius, radius)
        if active:
            p.setBrush(WHITE)
            p.drawEllipse(QPointF(bar.left() + bar.width() * frac, bar.center().y()), 5.5, 5.5)

    @staticmethod
    def _draw_speaker(p: QPainter, rect: QRectF, level: float, color: QColor) -> None:
        x0, cy = rect.left() + 1, rect.center().y()
        body = QPainterPath()
        body.moveTo(x0, cy - 2.5)
        body.lineTo(x0 + 3, cy - 2.5)
        body.lineTo(x0 + 7, cy - 6)
        body.lineTo(x0 + 7, cy + 6)
        body.lineTo(x0 + 3, cy + 2.5)
        body.lineTo(x0, cy + 2.5)
        body.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPath(body)
        pen = QPen(color, 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if level <= 0.001:
            p.drawLine(QPointF(x0 + 10, cy - 3), QPointF(x0 + 15, cy + 3))
            p.drawLine(QPointF(x0 + 10, cy + 3), QPointF(x0 + 15, cy - 3))
            return
        for radius, threshold in ((3.5, 0.0), (6.5, 0.5)):
            if level > threshold:
                p.drawArc(QRectF(x0 + 7 - radius, cy - radius, 2 * radius, 2 * radius), -50 * 16, 100 * 16)
