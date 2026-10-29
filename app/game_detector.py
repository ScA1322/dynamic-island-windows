"""Detects games / fullscreen applications in the foreground and decides whether to hide the island.

Signals combined (no single one is reliable on its own):
  * SHQueryUserNotificationState - reports exclusive D3D fullscreen and "busy" fullscreen apps;
  * foreground window geometry vs. its monitor, window styles (borderless fullscreen);
  * game hints: engine window classes (Unity, Unreal, SDL...) and game library install paths;
  * user lists: processes always treated as games, processes never hidden for.

The decision function `evaluate` is pure and GUI-free; `WindowProbe` gathers Win32 data;
`GameDetector` drives both from a slow Qt timer plus a WinEvent hook on foreground changes.
"""
from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from enum import Enum

from PySide6.QtCore import QObject, QTimer, Signal

log = logging.getLogger(__name__)

# SHQueryUserNotificationState results
QUNS_NOT_PRESENT = 1
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5
QUNS_QUIET_TIME = 6
QUNS_APP = 7

# shell surfaces: the desktop itself -> show
DESKTOP_CLASSES = {"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd"}
# transient shell UI (Alt+Tab, Task View, Start, notifications) -> keep the current decision
TRANSIENT_CLASSES = {
    "multitaskingviewframe", "xamlexplorerhostislandwindow", "taskswitcherwnd",
    "windows.ui.core.corewindow", "foregroundstaging", "notifyiconoverflowwindow",
    "topleveltaskbar", "#32768",
}
GAME_WINDOW_CLASSES = {
    "unitywndclass", "unrealwindow", "sdl_app", "glfw30", "lwjgl", "valve001",
    "riotwindowclass", "cryengine", "godotengine", "d3d window", "gamewindow",
}
GAME_PATH_MARKERS = (
    "\\steamapps\\", "\\epic games\\", "\\riot games\\", "\\gog games\\", "\\gog galaxy\\games\\",
    "\\xboxgames\\", "\\ubisoft game launcher\\games\\", "\\ea games\\", "\\origin games\\",
    "\\battlestate games\\", "\\wargaming.net\\", "\\games\\",
)


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    class_name: str
    title: str = ""
    process_name: str = ""          # lower-case, e.g. "game.exe"
    process_path: str = ""          # lower-case full path
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    monitor_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    monitor_id: int = 0
    minimized: bool = False
    maximized: bool = False
    visible: bool = True
    cloaked: bool = False
    has_caption: bool = False
    is_own: bool = False


@dataclass(frozen=True)
class DetectorConfig:
    hide_in_games: bool = True
    hide_in_fullscreen: bool = True
    manual_game_mode: bool = False
    excluded: frozenset[str] = field(default_factory=frozenset)
    games: frozenset[str] = field(default_factory=frozenset)


class Verdict(Enum):
    SHOW = "show"
    HIDE = "hide"
    KEEP = "keep"      # transient state, don't change anything


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str


def is_fullscreen(info: WindowInfo) -> bool:
    l, t, r, b = info.rect
    ml, mt, mr, mb = info.monitor_rect
    if mr <= ml or mb <= mt:
        return False
    covers = l <= ml and t <= mt and r >= mr and b >= mb
    # a maximized captioned window only covers the monitor when the taskbar auto-hides
    return covers and not (info.maximized and info.has_caption)


def looks_like_game(info: WindowInfo) -> bool:
    if info.class_name.lower() in GAME_WINDOW_CLASSES:
        return True
    path = info.process_path.lower()
    return any(marker in path for marker in GAME_PATH_MARKERS)


def evaluate(info: WindowInfo | None, notification_state: int | None, cfg: DetectorConfig,
             island_monitor: int | None = None) -> Decision:
    if cfg.manual_game_mode:
        return Decision(Verdict.HIDE, "игровой режим включён вручную")
    if info is None:
        return Decision(Verdict.KEEP, "нет активного окна")
    if info.is_own:
        return Decision(Verdict.KEEP, "собственное окно")
    cls = info.class_name.lower()
    if cls in TRANSIENT_CLASSES:
        return Decision(Verdict.KEEP, "системный интерфейс")
    if cls in DESKTOP_CLASSES:
        return Decision(Verdict.SHOW, "рабочий стол")
    if info.process_name and info.process_name in cfg.excluded:
        return Decision(Verdict.SHOW, f"{info.process_name} в исключениях")
    if info.minimized or not info.visible or info.cloaked:
        return Decision(Verdict.SHOW, "окно свёрнуто")
    if cfg.hide_in_games and info.process_name and info.process_name in cfg.games:
        return Decision(Verdict.HIDE, f"{info.process_name} в списке игр")

    fullscreen = is_fullscreen(info)
    if cfg.hide_in_games:
        if notification_state == QUNS_RUNNING_D3D_FULL_SCREEN:
            return Decision(Verdict.HIDE, "эксклюзивный полноэкранный режим (Direct3D)")
        if fullscreen and looks_like_game(info):
            return Decision(Verdict.HIDE, "полноэкранная игра")
    if cfg.hide_in_fullscreen and fullscreen:
        if island_monitor is None or info.monitor_id == island_monitor:
            return Decision(Verdict.HIDE, "полноэкранное приложение")
        return Decision(Verdict.SHOW, "полноэкранное приложение на другом мониторе")
    return Decision(Verdict.SHOW, "обычное окно")


class VisibilityDebouncer:
    """Hide immediately; show only after the 'show' verdict has been stable for show_delay seconds.

    Avoids flicker while Alt+Tabbing through a game or while a game switches display modes.
    """

    def __init__(self, show_delay: float = 0.6) -> None:
        self.show_delay = show_delay
        self.hidden = False
        self._show_since: float | None = None

    @property
    def pending_show(self) -> bool:
        return self._show_since is not None

    def update(self, verdict: Verdict, now: float) -> bool:
        if verdict is Verdict.HIDE:
            self.hidden = True
            self._show_since = None
        elif verdict is Verdict.SHOW and self.hidden:
            if self._show_since is None:
                self._show_since = now
            if now - self._show_since >= self.show_delay:
                self.hidden = False
                self._show_since = None
        elif verdict is Verdict.SHOW:
            self._show_since = None
        return self.hidden

    def reset(self) -> None:
        self.hidden = False
        self._show_since = None


# --------------------------------------------------------------------------- Win32 probe
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32")
try:
    _dwmapi = ctypes.WinDLL("dwmapi")
except OSError:  # pragma: no cover
    _dwmapi = None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_MAXIMIZE = 0x01000000
DWMWA_CLOAKED = 14
MONITOR_DEFAULTTONEAREST = 2


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.MonitorFromWindow.restype = wintypes.HMONITOR
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _rect_tuple(rect: wintypes.RECT) -> tuple[int, int, int, int]:
    return (rect.left, rect.top, rect.right, rect.bottom)


class WindowProbe:
    """Reads foreground window information through Win32 (physical pixels on both sides)."""

    def __init__(self) -> None:
        self._own_pid = os.getpid()
        self._path_cache: dict[int, str] = {}

    def process_path(self, pid: int) -> str:
        cached = self._path_cache.get(pid)
        if cached is not None:
            return cached
        path = ""
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            try:
                size = wintypes.DWORD(1024)
                buf = ctypes.create_unicode_buffer(size.value)
                if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    path = buf.value
            finally:
                _kernel32.CloseHandle(handle)
        if not path:
            try:
                import psutil

                path = psutil.Process(pid).name()
            except Exception:
                path = ""
        if len(self._path_cache) > 512:
            self._path_cache.clear()
        self._path_cache[pid] = path.lower()
        return path.lower()

    def window_info(self, hwnd: int) -> WindowInfo | None:
        if not hwnd:
            return None
        try:
            cls_buf = ctypes.create_unicode_buffer(256)
            _user32.GetClassNameW(hwnd, cls_buf, 256)
            title_buf = ctypes.create_unicode_buffer(256)
            _user32.GetWindowTextW(hwnd, title_buf, 256)

            rect = wintypes.RECT()
            if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None
            monitor = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            minfo = _MONITORINFO()
            minfo.cbSize = ctypes.sizeof(_MONITORINFO)
            if not monitor or not _user32.GetMonitorInfoW(monitor, ctypes.byref(minfo)):
                return None

            style = _user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            path = self.process_path(pid.value) if pid.value else ""

            cloaked = ctypes.c_int(0)
            if _dwmapi is not None:
                _dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED,
                                              ctypes.byref(cloaked), ctypes.sizeof(cloaked))
            return WindowInfo(
                hwnd=int(hwnd),
                class_name=cls_buf.value,
                title=title_buf.value,
                process_name=os.path.basename(path).lower(),
                process_path=path,
                rect=_rect_tuple(rect),
                monitor_rect=_rect_tuple(minfo.rcMonitor),
                monitor_id=int(monitor),
                minimized=bool(_user32.IsIconic(hwnd)),
                maximized=bool(style & WS_MAXIMIZE),
                visible=bool(_user32.IsWindowVisible(hwnd)),
                cloaked=bool(cloaked.value),
                has_caption=(style & WS_CAPTION) == WS_CAPTION,
                is_own=pid.value == self._own_pid,
            )
        except OSError as exc:  # transient Win32 failures (window destroyed mid-query, etc.)
            log.debug("window_info failed: %s", exc)
            return None

    def foreground(self) -> WindowInfo | None:
        return self.window_info(_user32.GetForegroundWindow())

    @staticmethod
    def notification_state() -> int | None:
        state = ctypes.c_int(0)
        try:
            if _shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
                return state.value
        except OSError:
            pass
        return None

    @staticmethod
    def monitor_of(hwnd: int) -> int | None:
        if not hwnd:
            return None
        monitor = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        return int(monitor) if monitor else None


# --------------------------------------------------------------------------- Qt driver
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

_WinEventProc = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                   wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
_user32.SetWinEventHook.restype = wintypes.HANDLE
_user32.SetWinEventHook.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.HMODULE, _WinEventProc,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.UINT]
_user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]


class GameDetector(QObject):
    """Emits hidden_changed(hidden, reason) when the island should disappear / come back."""

    hidden_changed = Signal(bool, str)

    def __init__(self, probe: WindowProbe | None = None, config: DetectorConfig | None = None,
                 island_hwnd_fn=None, interval_ms: int = 1000, show_delay: float = 0.6,
                 use_hooks: bool = True) -> None:
        super().__init__()
        self.probe = probe or WindowProbe()
        self.config = config or DetectorConfig()
        self._island_hwnd_fn = island_hwnd_fn or (lambda: 0)
        self._debouncer = VisibilityDebouncer(show_delay)
        self._use_hooks = use_hooks
        self._hooks: list[int] = []
        self._hook_proc = _WinEventProc(self._on_win_event)  # keep a reference: ctypes callback
        self.last_decision = Decision(Verdict.SHOW, "ещё не проверено")
        self.last_info: WindowInfo | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.check)
        self._soon = QTimer(self)
        self._soon.setSingleShot(True)
        self._soon.timeout.connect(self.check)

    @property
    def hidden(self) -> bool:
        return self._debouncer.hidden

    def start(self) -> None:
        self._timer.start()
        if self._use_hooks and not self._hooks:
            flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
            for lo, hi in ((EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
                           (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND)):
                handle = _user32.SetWinEventHook(lo, hi, None, self._hook_proc, 0, 0, flags)
                if handle:
                    self._hooks.append(handle)
                else:
                    log.warning("SetWinEventHook failed (%s); relying on polling", ctypes.get_last_error())
        self.check()

    def stop(self) -> None:
        self._timer.stop()
        self._soon.stop()
        for handle in self._hooks:
            _user32.UnhookWinEvent(handle)
        self._hooks.clear()

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def set_config(self, config: DetectorConfig) -> None:
        self.config = config
        self.check()

    def _on_win_event(self, *_args) -> None:
        # runs on the GUI thread (out-of-context hooks are delivered through its message loop)
        self._soon.start(60)

    def check(self) -> None:
        try:
            info = self.probe.foreground()
            quns = self.probe.notification_state()
            island_monitor = self.probe.monitor_of(self._island_hwnd_fn() or 0)
        except Exception as exc:
            log.debug("Foreground probe failed: %s", exc)
            return
        decision = evaluate(info, quns, self.config, island_monitor)
        self.apply(decision, info)

    def apply(self, decision: Decision, info: WindowInfo | None = None, now: float | None = None) -> None:
        was_hidden = self._debouncer.hidden
        hidden = self._debouncer.update(decision.verdict, time.monotonic() if now is None else now)
        if decision.verdict is not Verdict.KEEP:
            self.last_decision, self.last_info = decision, info
        if self._debouncer.pending_show:
            self._soon.start(int(self._debouncer.show_delay * 1000) + 20)
        if hidden != was_hidden:
            name = info.process_name if info else "-"
            log.info("Island %s: %s (%s)", "hidden" if hidden else "shown", decision.reason, name)
            self.hidden_changed.emit(hidden, decision.reason)
