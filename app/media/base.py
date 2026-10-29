"""Common interface for playback sources."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from app.media.models import Command, RepeatMode


class MediaSource(QObject):
    """A source of playback state that can also execute transport commands.

    state_changed(MediaState) is always emitted in the GUI thread.
    command_finished(command_value, ok, message) reports the outcome of send_command().
    """

    state_changed = Signal(object)
    command_finished = Signal(str, bool, str)

    name = "base"

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def send_command(self, command: Command) -> None:
        raise NotImplementedError

    def seek(self, position_ms: int) -> None:
        self.command_finished.emit("seek", False, "Перемотка недоступна")

    def set_volume(self, level: float) -> None:
        self.command_finished.emit("volume", False, "Громкость недоступна")

    def set_repeat(self, mode: RepeatMode) -> None:
        self.command_finished.emit("repeat", False, "Повтор недоступен")
