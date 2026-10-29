"""Game / fullscreen logic, tested without any GUI."""
import time

import pytest

from app.game_detector import (
    QUNS_ACCEPTS_NOTIFICATIONS, QUNS_RUNNING_D3D_FULL_SCREEN, DetectorConfig, GameDetector, Verdict,
    VisibilityDebouncer, WindowInfo, WindowProbe, evaluate, is_fullscreen, looks_like_game,
)
from tests.fakes import FakeProbe

MON = (0, 0, 2560, 1440)
CFG = DetectorConfig()


def win(cls="Chrome_WidgetWin_1", proc="chrome.exe", path=r"c:\program files\google\chrome.exe",
        rect=(100, 100, 1200, 900), monitor_id=1, **kw):
    return WindowInfo(hwnd=1, class_name=cls, process_name=proc, process_path=path, rect=rect,
                      monitor_rect=MON, monitor_id=monitor_id, **kw)


FULL = MON
MAXIMIZED = (-8, -8, 2568, 1400)          # maximized captioned window: taskbar still visible


def test_fullscreen_geometry():
    assert is_fullscreen(win(rect=FULL))
    assert is_fullscreen(win(rect=(-1, -1, 2561, 1441)))
    assert not is_fullscreen(win(rect=MAXIMIZED, maximized=True, has_caption=True))
    # auto-hidden taskbar: maximized captioned window covers the monitor but is not "fullscreen"
    assert not is_fullscreen(win(rect=(-8, -8, 2568, 1448), maximized=True, has_caption=True))
    assert not is_fullscreen(win(rect=(0, 0, 1280, 1440)))


def test_normal_window_shows():
    assert evaluate(win(), QUNS_ACCEPTS_NOTIFICATIONS, CFG, 1).verdict is Verdict.SHOW


def test_exclusive_fullscreen_hides():
    d = evaluate(win(cls="SomeGame", proc="game.exe", path=r"d:\x\game.exe", rect=FULL), QUNS_RUNNING_D3D_FULL_SCREEN, CFG, 1)
    assert d.verdict is Verdict.HIDE and "Direct3D" in d.reason


def test_borderless_fullscreen_game_hides_even_on_other_monitor():
    game = win(cls="UnityWndClass", proc="game.exe", path=r"d:\steamlibrary\steamapps\common\g\game.exe",
               rect=FULL, monitor_id=2)
    assert looks_like_game(game)
    assert evaluate(game, QUNS_ACCEPTS_NOTIFICATIONS, CFG, 1).verdict is Verdict.HIDE


def test_fullscreen_app_hides_only_on_island_monitor():
    video = win(rect=FULL)
    assert evaluate(video, 2, CFG, island_monitor=1).verdict is Verdict.HIDE
    assert evaluate(video, 2, CFG, island_monitor=7).verdict is Verdict.SHOW


def test_settings_switches():
    game = win(cls="UnrealWindow", proc="g.exe", path=r"c:\games\g.exe", rect=FULL)
    no_games = DetectorConfig(hide_in_games=False, hide_in_fullscreen=False)
    assert evaluate(game, QUNS_RUNNING_D3D_FULL_SCREEN, no_games, 1).verdict is Verdict.SHOW
    only_games = DetectorConfig(hide_in_fullscreen=False)
    assert evaluate(win(rect=FULL), 2, only_games, 1).verdict is Verdict.SHOW          # browser F11
    assert evaluate(game, 5, only_games, 1).verdict is Verdict.HIDE


def test_lists():
    cfg = DetectorConfig(excluded=frozenset({"vlc.exe"}), games=frozenset({"windowed.exe"}))
    assert evaluate(win(proc="vlc.exe", rect=FULL), 2, cfg, 1).verdict is Verdict.SHOW
    assert evaluate(win(proc="windowed.exe"), 5, cfg, 1).verdict is Verdict.HIDE        # listed, even windowed


def test_manual_game_mode():
    assert evaluate(None, None, DetectorConfig(manual_game_mode=True)).verdict is Verdict.HIDE


@pytest.mark.parametrize("info,verdict", [
    (None, Verdict.KEEP),
    (win(is_own=True), Verdict.KEEP),
    (win(cls="XamlExplorerHostIslandWindow", rect=FULL), Verdict.KEEP),       # Alt+Tab on Windows 11
    (win(cls="MultitaskingViewFrame", rect=FULL), Verdict.KEEP),
    (win(cls="WorkerW", proc="explorer.exe", rect=FULL), Verdict.SHOW),       # desktop covers the screen
    (win(cls="Progman", proc="explorer.exe", rect=FULL), Verdict.SHOW),
    (win(cls="UnityWndClass", rect=FULL, minimized=True), Verdict.SHOW),      # game minimized
    (win(rect=FULL, cloaked=True), Verdict.SHOW),
])
def test_special_windows(info, verdict):
    assert evaluate(info, QUNS_ACCEPTS_NOTIFICATIONS, CFG, 1).verdict is verdict


def test_debouncer_hides_immediately_and_shows_after_delay():
    d = VisibilityDebouncer(show_delay=0.5)
    assert d.update(Verdict.HIDE, 0.0) is True
    assert d.update(Verdict.SHOW, 1.0) is True and d.pending_show
    assert d.update(Verdict.KEEP, 1.2) is True                 # Alt+Tab overlay in between
    assert d.update(Verdict.HIDE, 1.3) is True and not d.pending_show
    assert d.update(Verdict.SHOW, 2.0) is True
    assert d.update(Verdict.SHOW, 2.6) is False


def test_detector_hide_and_reappear(qtbot):
    probe = FakeProbe(info=win(cls="UnityWndClass", proc="game.exe", rect=FULL))
    detector = GameDetector(probe=probe, interval_ms=50, show_delay=0.15, use_hooks=False)
    events = []
    detector.hidden_changed.connect(lambda hidden, reason: events.append(hidden))
    detector.start()
    assert detector.hidden and events == [True]
    probe.info = win(cls="WorkerW", proc="explorer.exe", rect=FULL)    # back to the desktop
    qtbot.waitUntil(lambda: events == [True, False], timeout=2000)
    probe.info = win(cls="UnityWndClass", proc="game.exe", rect=FULL)
    qtbot.waitUntil(lambda: events == [True, False, True], timeout=2000)
    detector.stop()
    assert not detector.running


def test_detector_survives_probe_errors(qtbot):
    class Broken(FakeProbe):
        def foreground(self):
            raise OSError("transient")

    detector = GameDetector(probe=Broken(), interval_ms=50, use_hooks=False)
    detector.start()
    qtbot.wait(120)
    assert not detector.hidden
    detector.stop()


def test_real_probe_reads_foreground_window():
    probe = WindowProbe()
    info = probe.foreground()
    # the test runner may have no foreground window (e.g. locked session); otherwise data must be sane
    if info is not None:
        l, t, r, b = info.monitor_rect
        assert r > l and b > t
        assert isinstance(info.class_name, str)
    assert probe.notification_state() in (None, 1, 2, 3, 4, 5, 6, 7)


def test_real_hooks_install_and_uninstall(qtbot):
    detector = GameDetector(probe=FakeProbe(info=win()), use_hooks=True)
    detector.start()
    assert detector._hooks                                            # WinEvent hooks are registered
    detector.stop()
    assert not detector._hooks
