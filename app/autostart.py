"""Start-with-Windows via HKCU\\...\\Run (per-user, no admin rights, one named value = no duplicates)."""
from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "DynamicIsland"


def launch_command() -> str:
    if getattr(sys, "frozen", False):           # PyInstaller build
        return f'"{Path(sys.executable).resolve()}"'
    exe = Path(sys.executable).resolve()
    pythonw = exe.with_name("pythonw.exe")      # no console window at logon
    interpreter = pythonw if pythonw.exists() else exe
    main = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{interpreter}" "{main}"'


class AutostartManager:
    def __init__(self, key_path: str = RUN_KEY, value_name: str = VALUE_NAME,
                 command_factory=launch_command, root=winreg.HKEY_CURRENT_USER) -> None:
        self.key_path = key_path
        self.value_name = value_name
        self.command_factory = command_factory
        self.root = root

    def current_command(self) -> str | None:
        try:
            with winreg.OpenKey(self.root, self.key_path, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, self.value_name)
                return str(value)
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.warning("Cannot read autostart entry: %s", exc)
            return None

    def is_enabled(self) -> bool:
        return self.current_command() is not None

    def enable(self) -> bool:
        command = self.command_factory()
        try:
            with winreg.CreateKeyEx(self.root, self.key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, self.value_name, 0, winreg.REG_SZ, command)
            log.info("Autostart enabled")
            return True
        except OSError as exc:
            log.error("Cannot enable autostart: %s", exc)
            return False

    def disable(self) -> bool:
        try:
            with winreg.OpenKey(self.root, self.key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, self.value_name)
            log.info("Autostart disabled")
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.error("Cannot disable autostart: %s", exc)
            return False
        return True

    def sync(self, enabled: bool) -> bool:
        """Make the registry match the setting; also repairs the path if the app was moved."""
        if not enabled:
            return self.disable() if self.is_enabled() else True
        if self.current_command() == self.command_factory():
            return True
        return self.enable()
