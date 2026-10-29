"""Test doubles shared by several test modules."""
from __future__ import annotations

from PySide6.QtCore import Signal

from app.media.base import MediaSource
from app.media.models import Command, MediaState


class FakeSource(MediaSource):
    def __init__(self, name: str = "smtc") -> None:
        super().__init__()
        self.name = name
        self.commands: list[Command] = []
        self.running = False
        self.stop_calls = 0

    def start(self) -> None:
        self.running = True

    def stop(self) -> bool:
        self.running = False
        self.stop_calls += 1
        return True

    def send_command(self, command: Command) -> None:
        self.commands.append(command)

    def push(self, state: MediaState) -> None:
        self.state_changed.emit(state)


class FakeApiSource(FakeSource):
    status_changed = Signal(str, str)
    open_url = Signal(str)

    def __init__(self) -> None:
        super().__init__("webapi")
        self.connected = False
        self.active_calls: list[bool] = []
        self.configured: list[tuple[str, int]] = []
        from app.media.spotify_api import SpotifyStatus

        self.status = SpotifyStatus.NOT_CONFIGURED
        self.status_message = ""

    def set_active(self, active: bool) -> None:
        self.active_calls.append(active)

    def configure(self, client_id: str, port: int) -> None:
        self.configured.append((client_id, port))

    def connect_account(self) -> None:
        pass

    def disconnect_account(self) -> None:
        pass

    def check_connection(self) -> None:
        pass


class FakeProbe:
    def __init__(self, info=None, quns=5, monitor=1) -> None:
        self.info = info
        self.quns = quns
        self.monitor = monitor

    def foreground(self):
        return self.info

    def notification_state(self):
        return self.quns

    def monitor_of(self, _hwnd):
        return self.monitor


class FakeAutostart:
    def __init__(self) -> None:
        self.enabled = False
        self.sync_calls: list[bool] = []

    def sync(self, enabled: bool) -> bool:
        self.sync_calls.append(enabled)
        self.enabled = enabled
        return True

    def is_enabled(self) -> bool:
        return self.enabled
