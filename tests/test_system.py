"""Tray, autostart, single instance, overlay visibility and shutdown."""
import subprocess
import sys
import uuid
import winreg

import pytest
import win32con
import win32gui

from app.application import IslandApplication
from app.autostart import AutostartManager, launch_command
from app.config import load_settings
from app.game_detector import GameDetector, WindowInfo
from app.media.controller import MediaController
from app.media.models import Command, MediaState, PlaybackStatus
from app.single_instance import ActivationServer, SingleInstance, notify_running_instance
from app.system_tray import TrayController
from tests.fakes import FakeApiSource, FakeAutostart, FakeProbe, FakeSource

TEST_KEY = r"Software\DynamicIslandTests"


# --------------------------------------------------------------------------- autostart
@pytest.fixture
def autostart():
    manager = AutostartManager(key_path=TEST_KEY, value_name="Island-" + uuid.uuid4().hex[:8],
                               command_factory=lambda: '"C:\\Apps\\DynamicIsland.exe"')
    yield manager
    manager.disable()
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
    except OSError:
        pass


def test_autostart_enable_disable(autostart):
    assert not autostart.is_enabled()
    assert autostart.enable() and autostart.is_enabled()
    assert autostart.current_command() == '"C:\\Apps\\DynamicIsland.exe"'
    assert autostart.enable()                                     # idempotent: one named value, no duplicates
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
        assert winreg.QueryInfoKey(key)[1] == 1
    assert autostart.disable() and not autostart.is_enabled()
    assert autostart.disable()                                    # disabling twice is fine


def test_autostart_sync_repairs_moved_app(autostart):
    autostart.enable()
    autostart.command_factory = lambda: '"D:\\New\\DynamicIsland.exe"'
    autostart.sync(True)
    assert autostart.current_command() == '"D:\\New\\DynamicIsland.exe"'
    autostart.sync(False)
    assert not autostart.is_enabled()


def test_launch_command_from_source():
    cmd = launch_command()
    assert cmd.endswith('main.py"') and "python" in cmd.lower()


# --------------------------------------------------------------------------- single instance
def test_single_instance_blocks_second():
    name = "DynamicIslandTest." + uuid.uuid4().hex
    first, second = SingleInstance(name), SingleInstance(name)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_single_instance_across_processes():
    name = "DynamicIslandTest." + uuid.uuid4().hex
    first = SingleInstance(name)
    assert first.acquire()
    code = ("import sys; sys.path.insert(0, r'%s'); from app.single_instance import SingleInstance; "
            "sys.exit(0 if not SingleInstance(%r).acquire() else 1)") % (sys.path[0], name)
    assert subprocess.run([sys.executable, "-c", code], timeout=60).returncode == 0
    first.release()


def test_second_launch_activates_first(qtbot):
    name = "DynamicIslandTest." + uuid.uuid4().hex
    server = ActivationServer(name)
    assert server.listen()
    code = ("import sys; sys.path.insert(0, r'%s'); from PySide6.QtCore import QCoreApplication; "
            "app = QCoreApplication([]); from app.single_instance import notify_running_instance; "
            "sys.exit(0 if notify_running_instance(%r) else 1)") % (sys.path[0], name)
    with qtbot.waitSignal(server.activated, timeout=10000):
        second = subprocess.Popen([sys.executable, "-c", code])    # a real second launch
    assert second.wait(30) == 0
    server.close()
    assert not notify_running_instance(name, timeout_ms=200)


# --------------------------------------------------------------------------- tray
def test_tray_menu(qtbot):
    from app.resources import app_icon

    tray = TrayController(app_icon())
    texts = [a.text() for a in tray.menu.actions() if not a.isSeparator()]
    assert texts == ["Скрыть Dynamic Island", "Настройки…", "Подключить Spotify Web API…",
                     "Игровой режим (скрыть)", "Запускать вместе с Windows", "Выход"]
    got = []
    tray.open_settings.connect(lambda: got.append("settings"))
    tray.quit_requested.connect(lambda: got.append("quit"))
    tray.toggle_game_mode.connect(lambda on: got.append(("game", on)))
    tray.action_settings.trigger()
    tray.action_game_mode.trigger()
    tray.action_quit.trigger()
    assert got == ["settings", ("game", True), "quit"]
    tray.update_state(overlay_visible=False, game_mode=False, autostart=True, spotify_connected=True)
    assert tray.action_overlay.text() == "Показать Dynamic Island"
    assert tray.action_autostart.isChecked() and not tray.action_game_mode.isChecked()
    assert tray.action_spotify.text().startswith("Переподключить")


# --------------------------------------------------------------------------- whole application
def _game():
    return WindowInfo(hwnd=5, class_name="UnityWndClass", process_name="game.exe", process_path=r"c:\games\game.exe",
                      rect=(0, 0, 1920, 1080), monitor_rect=(0, 0, 1920, 1080), monitor_id=1)


def _desktop():
    return WindowInfo(hwnd=6, class_name="WorkerW", process_name="explorer.exe",
                      rect=(0, 0, 1920, 1080), monitor_rect=(0, 0, 1920, 1080), monitor_id=1)


@pytest.fixture
def island_app(qapp, tmp_path):
    smtc, api = FakeSource("smtc"), FakeApiSource()
    probe = FakeProbe(info=_desktop())
    detector = GameDetector(probe=probe, interval_ms=50, show_delay=0.1, use_hooks=False)
    app = IslandApplication(qapp, settings_file=tmp_path / "settings.json",
                            media=MediaController(smtc, api), detector=detector, autostart=FakeAutostart())
    app.start()
    yield app, smtc, api, probe
    app.shutdown()


def test_overlay_hides_in_game_and_returns(qtbot, island_app):
    app, smtc, _api, probe = island_app
    win = app.window
    assert win.isVisible() and win.pointer_polling
    probe.info = _game()
    qtbot.waitUntil(lambda: not win.isVisible(), timeout=2000)
    assert not win.pointer_polling and not win.view.timers_running     # nothing runs while gaming
    smtc.push(MediaState(source="smtc", player_available=True, title="Still playing",
                         status=PlaybackStatus.PLAYING))
    assert smtc.commands == []                                        # hiding never pauses music
    probe.info = _desktop()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=2000)
    assert win.view.state.title == "Still playing"


def test_window_flags_click_through_and_no_taskbar(qtbot, island_app):
    app, *_ = island_app
    win = app.window
    ex = win32gui.GetWindowLong(int(win.winId()), win32con.GWL_EXSTYLE)
    assert ex & win32con.WS_EX_TOOLWINDOW and ex & win32con.WS_EX_NOACTIVATE and ex & win32con.WS_EX_TOPMOST
    assert ex & win32con.WS_EX_TRANSPARENT                            # compact: clicks fall through
    win._pointer_timer.stop()
    win.pointer_moved(True)
    ex = win32gui.GetWindowLong(int(win.winId()), win32con.GWL_EXSTYLE)
    assert not ex & win32con.WS_EX_TRANSPARENT and win.hover.expanded  # hovered: buttons clickable
    win.pointer_moved(False)
    ex = win32gui.GetWindowLong(int(win.winId()), win32con.GWL_EXSTYLE)
    assert ex & win32con.WS_EX_TRANSPARENT


def test_window_is_centered_at_top(qtbot, island_app):
    app, *_ = island_app
    win = app.window
    screen = win.target_screen().geometry()
    geo = win.geometry()
    assert geo.top() == screen.top()
    assert abs(geo.center().x() - screen.center().x()) <= 1


def test_button_click_sends_command(qtbot, island_app):
    from PySide6.QtCore import QPoint, Qt
    from app.media.models import Capabilities

    app, smtc, *_ = island_app
    win = app.window
    win._pointer_timer.stop()
    smtc.push(MediaState(source="smtc", player_available=True, title="Song", status=PlaybackStatus.PLAYING,
                         caps=Capabilities(True, True, True, True)))
    win.pointer_moved(True)
    qtbot.waitUntil(lambda: not win.view.expansion.running, timeout=2000)
    rect = win.view.button_rects(win.view.capsule_rect(win.width()))[Command.NEXT]
    qtbot.mouseClick(win, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
    assert smtc.commands == [Command.NEXT]


def test_manual_hide_and_settings_persist(qtbot, island_app, tmp_path):
    app, *_ = island_app
    app.toggle_overlay()
    assert not app.window.wanted_visible
    assert load_settings(tmp_path / "settings.json").overlay_visible is False
    app.toggle_overlay()
    qtbot.waitUntil(lambda: app.window.isVisible(), timeout=1000)
    app.update_settings(game_mode_manual=True)
    qtbot.waitUntil(lambda: not app.window.isVisible(), timeout=1000)
    app.update_settings(game_mode_manual=False, compact_width=222)
    assert load_settings(tmp_path / "settings.json").compact_width == 222
    assert app.tray.action_game_mode.isChecked() is False


def test_settings_window_close_does_not_quit(qtbot, island_app):
    app, *_ = island_app
    app.show_settings()
    assert app.settings_window.isVisible()
    app.settings_window.close()
    assert not app.settings_window.isVisible()
    assert app.window.isVisible()


def test_settings_window_applies_values(qtbot, island_app, tmp_path):
    app, *_ = island_app
    sw = app.settings_window
    sw.expanded_width.setValue(520)
    sw.excluded.setPlainText("VLC.exe\n")
    sw.save_button.click()
    saved = load_settings(tmp_path / "settings.json")
    assert saved.expanded_width == 520 and saved.excluded_processes == ["vlc.exe"]
    assert app.detector.config.excluded == frozenset({"vlc.exe"})


def test_autostart_toggle_from_tray(qtbot, island_app):
    app, *_ = island_app
    app.tray.action_autostart.trigger()
    assert app.settings.autostart and app.autostart.enabled
    app.tray.action_autostart.trigger()
    assert not app.settings.autostart and not app.autostart.enabled


def test_clean_shutdown(qtbot, island_app):
    app, smtc, api, _ = island_app
    app.shutdown()
    assert smtc.stop_calls == 1 and api.stop_calls == 1
    assert not app.detector.running
    assert not app.window.isVisible() and not app.window.pointer_polling
    assert not app.window.view.timers_running
    assert not app.tray.tray.isVisible()
    app.shutdown()                                                    # idempotent
    assert smtc.stop_calls == 1


def test_real_workers_stop(qtbot, tmp_path):
    """The real SMTC and Web API threads start and stop within the timeout."""
    from app.media.cover_cache import CoverCache
    from app.media.smtc import SmtcSource
    from app.media.spotify_api import SpotifyWebApiSource, TokenStore

    cache = CoverCache(tmp_path / "covers")
    smtc = SmtcSource(cache)
    api = SpotifyWebApiSource(TokenStore(tmp_path / "t.bin"), cache)
    ctl = MediaController(smtc, api)
    ctl.start()
    api.configure("", 8765)
    qtbot.waitUntil(lambda: smtc.is_running() and api.is_running(), timeout=3000)
    with qtbot.waitSignal(api.status_changed, timeout=3000):
        api.configure("abc123", 8765)
    assert ctl.stop()
    assert not smtc.is_running() and not api.is_running()
