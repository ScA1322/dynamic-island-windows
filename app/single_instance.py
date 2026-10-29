"""Single-instance guard (named mutex) plus a local socket so a second launch opens the settings
of the already running instance instead of starting a duplicate."""
from __future__ import annotations

import getpass
import hashlib
import os
import logging
import re

import win32api
import win32event
import winerror
from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)


def _user_suffix() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", user)


def default_name() -> str:
    name = f"DynamicIsland.{_user_suffix()}"
    home = os.environ.get("DYNAMIC_ISLAND_HOME")
    if home:   # a separate data folder (portable copy / tests) is a separate instance
        name += "." + hashlib.sha1(os.path.abspath(home).lower().encode("utf-8")).hexdigest()[:10]
    return name


class SingleInstance:
    def __init__(self, name: str | None = None) -> None:
        self.name = name or default_name()
        self._handle = None

    def acquire(self) -> bool:
        handle = win32event.CreateMutex(None, False, f"Local\\{self.name}")
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            win32api.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            win32api.CloseHandle(self._handle)
            self._handle = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None


class ActivationServer(QObject):
    """Receives 'show' messages from later launches."""

    activated = Signal()

    def __init__(self, name: str | None = None) -> None:
        super().__init__()
        self.name = (name or default_name()) + ".activate"
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_connection)
        self._clients: set[QLocalSocket] = set()

    def listen(self) -> bool:
        QLocalServer.removeServer(self.name)
        ok = self._server.listen(self.name)
        if not ok:
            log.warning("Activation server failed: %s", self._server.errorString())
        return ok

    def close(self) -> None:
        for sock in list(self._clients):
            self._drop(sock)
        self._server.close()

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            self._clients.add(sock)
            sock.readyRead.connect(self._on_ready_read)
            sock.disconnected.connect(self._on_disconnected)
            if sock.bytesAvailable():           # data may arrive before readyRead is connected
                self._read(sock)

    def _on_ready_read(self) -> None:
        sock = self.sender()
        if sock in self._clients:
            self._read(sock)

    def _on_disconnected(self) -> None:
        sock = self.sender()
        if sock in self._clients:
            self._drop(sock)

    def _read(self, sock: QLocalSocket) -> None:
        if bytes(sock.readAll()).strip() == b"show":
            self.activated.emit()

    def _drop(self, sock: QLocalSocket) -> None:
        self._clients.discard(sock)
        # detach before teardown: a socket being destroyed emits signals of its own
        sock.readyRead.disconnect(self._on_ready_read)
        sock.disconnected.disconnect(self._on_disconnected)
        sock.abort()
        sock.deleteLater()


def notify_running_instance(name: str | None = None, timeout_ms: int = 1000) -> bool:
    sock = QLocalSocket()
    sock.connectToServer((name or default_name()) + ".activate")
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(b"show\n")
    sock.flush()
    sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    return True
