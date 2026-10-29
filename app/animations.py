"""Animation primitives for the island.

AnimatedValue - a float Qt property driven by QPropertyAnimation. Retargeting mid-flight starts
                from the current value, so hovering again during a collapse reverses smoothly.
HoverIntent   - turns raw pointer enter/leave into expand/collapse with a cancellable delay.
"""
from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QObject, QPropertyAnimation, QTimer, Signal


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = clamp01((x - edge0) / (edge1 - edge0)) if edge1 != edge0 else float(x >= edge1)
    return t * t * (3 - 2 * t)


def ease_curve(kind: QEasingCurve.Type, overshoot: float | None = None) -> QEasingCurve:
    curve = QEasingCurve(kind)
    if overshoot is not None:
        curve.setOvershoot(overshoot)
    return curve


class AnimatedValue(QObject):
    changed = Signal(float)

    def __init__(self, initial: float = 0.0, duration_ms: int = 280,
                 easing: QEasingCurve | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._value = float(initial)
        self._target = float(initial)
        self.duration_ms = duration_ms
        self.easing = easing or QEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim = QPropertyAnimation(self, b"value", self)

    def _get(self) -> float:
        return self._value

    def _set(self, value: float) -> None:
        self._value = float(value)
        self.changed.emit(self._value)

    value = Property(float, _get, _set)

    @property
    def target(self) -> float:
        return self._target

    @property
    def running(self) -> bool:
        return self._anim.state() == QPropertyAnimation.State.Running

    def animate_to(self, target: float, duration_ms: int | None = None,
                   easing: QEasingCurve | None = None, span: float = 1.0) -> None:
        """Animate from the current value; duration shrinks with the remaining distance."""
        target = float(target)
        self._target = target
        if abs(target - self._value) < 1e-4:
            self._anim.stop()
            if self._value != target:
                self._set(target)
            return
        full = self.duration_ms if duration_ms is None else duration_ms
        distance = min(1.0, abs(target - self._value) / span) if span else 1.0
        self._anim.stop()
        self._anim.setStartValue(self._value)
        self._anim.setEndValue(target)
        self._anim.setDuration(max(90, int(full * max(distance, 0.4))))
        self._anim.setEasingCurve(easing or self.easing)
        self._anim.start()

    def jump(self, value: float) -> None:
        self._anim.stop()
        self._target = float(value)
        self._set(value)

    def stop(self) -> None:
        self._anim.stop()


class HoverIntent(QObject):
    expand_requested = Signal()
    collapse_requested = Signal()

    def __init__(self, collapse_delay_ms: int = 500, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.expanded = False
        self.hovered = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(collapse_delay_ms)
        self._timer.timeout.connect(self._collapse_now)

    @property
    def collapse_pending(self) -> bool:
        return self._timer.isActive()

    def set_delay(self, ms: int) -> None:
        self._timer.setInterval(max(0, int(ms)))

    def pointer_entered(self) -> None:
        self.hovered = True
        self._timer.stop()                      # pointer came back: cancel pending collapse
        if not self.expanded:
            self.expanded = True
            self.expand_requested.emit()

    def pointer_left(self) -> None:
        self.hovered = False
        if self.expanded and not self._timer.isActive():
            self._timer.start()

    def force_collapse(self) -> None:
        self.hovered = False
        self._timer.stop()
        self._collapse_now()

    def _collapse_now(self) -> None:
        if self.expanded:
            self.expanded = False
            self.collapse_requested.emit()
