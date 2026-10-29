"""Seeking by the progress bar and Spotify volume control."""
from PySide6.QtCore import QPoint, QPointF, Qt

from app.config import Settings
from app.island_window import IslandWindow
from app.media.controller import MediaController
from app.media.models import Capabilities, MediaState, PlaybackStatus
from app.media.spotify_api import playback_to_state
from tests.fakes import FakeApiSource, FakeSource

CAPS = Capabilities(True, True, True, True, seek=True)


def track(**kw):
    defaults = dict(source="smtc", player_available=True, title="Song", artist="A", status=PlaybackStatus.PLAYING,
                    position_ms=10_000, duration_ms=200_000, caps=CAPS)
    defaults.update(kw)
    return MediaState(**defaults)


class FakeVolume:
    def __init__(self, level=0.6):
        self.level = level
        self.sets = []

    def get(self):
        return self.level

    def set(self, level):
        self.sets.append(round(level, 3))
        self.level = level
        return True


class SeekSource(FakeSource):
    def __init__(self, name="smtc"):
        super().__init__(name)
        self.seeks = []

    def seek(self, position_ms):
        self.seeks.append(position_ms)


# --------------------------------------------------------------------------- controller
def test_controller_seek_routes_to_active_source(qtbot):
    smtc = SeekSource()
    ctl = MediaController(smtc, None)
    smtc.push(track())
    ctl.seek(42_000)
    assert smtc.seeks == [42_000]


def test_controller_refuses_seek_without_capability(qtbot):
    smtc = SeekSource()
    ctl = MediaController(smtc, None)
    notices = []
    ctl.notice.connect(notices.append)
    smtc.push(track(caps=Capabilities(True, True, True, True, seek=False)))
    ctl.seek(1000)
    assert smtc.seeks == [] and notices


def test_failed_seek_restores_real_position(qtbot):
    smtc = SeekSource()
    ctl = MediaController(smtc, None)
    smtc.push(track())
    states = []
    ctl.state_changed.connect(states.append)
    smtc.command_finished.emit("seek", False, "Spotify отклонил перемотку")
    assert states and states[-1].position_ms == 10_000


def test_local_volume_for_smtc_and_api_volume_for_webapi(qtbot):
    smtc, api, vol = SeekSource(), FakeApiSource(), FakeVolume(0.6)
    api.volumes = []
    api.set_volume = api.volumes.append
    ctl = MediaController(smtc, api, vol)
    smtc.push(track())
    assert ctl.volume() == 0.6
    ctl.set_volume(0.3)
    assert vol.sets == [0.3] and api.volumes == []
    api.connected = True
    smtc.push(MediaState(source="smtc", player_available=False))
    api.push(track(source="webapi", volume=0.8))
    assert ctl.volume() == 0.8                       # device volume reported by the Web API
    ctl.set_volume(0.5)
    assert api.volumes == [0.5] and vol.sets == [0.3]


def test_webapi_payload_volume_and_seek_caps():
    payload = {"is_playing": True, "progress_ms": 1, "device": {"volume_percent": 35, "supports_volume": True},
               "item": {"name": "x", "duration_ms": 10, "artists": [], "album": {}},
               "actions": {"disallows": {"seeking": True}}}
    state = playback_to_state(payload, None, 0.0)
    assert state.volume == 0.35 and not state.caps.seek
    payload["device"]["supports_volume"] = False
    assert playback_to_state(payload, None, 0.0).volume is None


# --------------------------------------------------------------------------- window interaction
def make_window(qtbot, volume=0.5):
    win = IslandWindow(Settings())
    qtbot.addWidget(win)
    win.volume_provider = lambda: volume
    win.set_media_state(track())
    win.hover.pointer_entered()
    qtbot.waitUntil(lambda: not win.view.expansion.running, timeout=2000)
    return win


def bar_point(win, frac):
    bar = win.view.progress_layout(win.view.capsule_rect(win.width()))["bar"]
    return QPoint(round(bar.left() + bar.width() * frac), round(bar.center().y()))


def test_click_on_progress_bar_seeks(qtbot):
    win = make_window(qtbot)
    seeks = []
    win.seek_requested.connect(seeks.append)
    qtbot.mouseClick(win, Qt.MouseButton.LeftButton, pos=bar_point(win, 0.5))
    assert len(seeks) == 1 and abs(seeks[0] - 100_000) < 2_000
    assert abs(win.view.state.position_at() - seeks[0]) < 1_000      # shown immediately


def test_drag_scrubs_and_seeks_once_on_release(qtbot):
    win = make_window(qtbot)
    seeks = []
    win.seek_requested.connect(seeks.append)
    qtbot.mousePress(win, Qt.MouseButton.LeftButton, pos=bar_point(win, 0.2))
    qtbot.mouseMove(win, bar_point(win, 0.6))
    win.mouseMoveEvent(_move_event(win, bar_point(win, 0.6)))
    assert seeks == [] and abs(win.view.scrub_ms - 120_000) < 2_000   # preview only while dragging
    qtbot.mouseRelease(win, Qt.MouseButton.LeftButton, pos=bar_point(win, 0.75))
    assert len(seeks) == 1 and abs(seeks[0] - 150_000) < 2_000
    assert win.view.dragging is None and win.view.scrub_ms is None


def test_wheel_changes_volume(qtbot):
    win = make_window(qtbot, volume=0.5)
    volumes = []
    win.volume_requested.connect(volumes.append)
    for _ in range(2):
        win.wheelEvent(_wheel_event(win, 120))
    assert [round(v, 2) for v in volumes] == [0.55, 0.6]
    assert win.view.in_volume_mode
    win.wheelEvent(_wheel_event(win, -120 * 30))
    assert volumes[-1] == 0.0                        # clamped


def test_toggle_volume_row_and_drag_volume(qtbot):
    win = make_window(qtbot, volume=0.5)
    volumes = []
    win.volume_requested.connect(volumes.append)
    toggle = win.view.progress_layout(win.view.capsule_rect(win.width()))["toggle"]
    assert toggle is not None
    qtbot.mouseClick(win, Qt.MouseButton.LeftButton, pos=toggle.center().toPoint())
    assert win.view.in_volume_mode
    qtbot.mouseClick(win, Qt.MouseButton.LeftButton, pos=bar_point(win, 0.25))
    assert abs(volumes[-1] - 0.25) < 0.03
    qtbot.mouseClick(win, Qt.MouseButton.LeftButton, pos=toggle.center().toPoint())
    assert not win.view.in_volume_mode


def test_mute_button_restores_previous_level(qtbot):
    win = make_window(qtbot, volume=0.7)
    volumes = []
    win.volume_requested.connect(volumes.append)
    win.view.enter_volume_mode()
    left = win.view.progress_layout(win.view.capsule_rect(win.width()))["left"].center().toPoint()
    qtbot.mouseClick(win, Qt.MouseButton.LeftButton, pos=left)
    qtbot.mouseClick(win, Qt.MouseButton.LeftButton, pos=left)
    assert volumes == [0.0, 0.7]


def test_no_volume_icon_when_unavailable(qtbot):
    win = make_window(qtbot, volume=None)
    assert win.view.progress_layout(win.view.capsule_rect(win.width()))["toggle"] is None
    win.view.enter_volume_mode()
    assert not win.view.in_volume_mode


def test_volume_row_returns_to_progress(qtbot):
    win = make_window(qtbot)
    win.view._volume_timer.setInterval(100)
    win.view.enter_volume_mode()
    qtbot.waitUntil(lambda: not win.view.in_volume_mode, timeout=1500)


def test_seek_disabled_when_player_cannot_seek(qtbot):
    win = make_window(qtbot)
    win.set_media_state(track(caps=Capabilities(True, True, True, True, seek=False)))
    seeks = []
    win.seek_requested.connect(seeks.append)
    qtbot.mouseClick(win, Qt.MouseButton.LeftButton, pos=bar_point(win, 0.5))
    assert seeks == []


# --------------------------------------------------------------------------- helpers
def _move_event(win, point):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QMouseEvent

    pos = QPointF(point)
    return QMouseEvent(QEvent.Type.MouseMove, pos, QPointF(win.mapToGlobal(point)), Qt.MouseButton.NoButton,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)


def _wheel_event(win, delta):
    from PySide6.QtCore import QPoint as P
    from PySide6.QtGui import QWheelEvent

    pos = QPointF(win.width() / 2, 40)
    return QWheelEvent(pos, QPointF(win.mapToGlobal(pos.toPoint())), P(0, 0), P(0, delta),
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)


# --------------------------------------------------------------------------- repeat
from app.media.models import RepeatMode  # noqa: E402
from app.media.smtc import map_repeat  # noqa: E402


class RepeatSource(SeekSource):
    def __init__(self, name="smtc"):
        super().__init__(name)
        self.repeats = []

    def set_repeat(self, mode):
        self.repeats.append(mode)


REPEAT_CAPS = Capabilities(True, True, True, True, seek=True, repeat=True)


def test_repeat_cycle_matches_spotify():
    assert RepeatMode.OFF.next() is RepeatMode.CONTEXT
    assert RepeatMode.CONTEXT.next() is RepeatMode.TRACK
    assert RepeatMode.TRACK.next() is RepeatMode.OFF


def test_smtc_repeat_mapping():
    assert map_repeat(0) is RepeatMode.OFF
    assert map_repeat(1) is RepeatMode.TRACK
    assert map_repeat(2) is RepeatMode.CONTEXT
    assert map_repeat(None) is None and map_repeat("x") is None


def test_webapi_repeat_state():
    payload = {"is_playing": True, "repeat_state": "context",
               "item": {"name": "x", "duration_ms": 10, "artists": [], "album": {}}}
    state = playback_to_state(payload, None, 0.0)
    assert state.repeat is RepeatMode.CONTEXT and state.caps.repeat
    payload["actions"] = {"disallows": {"toggling_repeat_context": True, "toggling_repeat_track": True}}
    assert not playback_to_state(payload, None, 0.0).caps.repeat


def test_controller_repeat_routing_and_revert(qtbot):
    smtc = RepeatSource()
    ctl = MediaController(smtc, None)
    smtc.push(track(caps=REPEAT_CAPS, repeat=RepeatMode.OFF))
    ctl.set_repeat(RepeatMode.CONTEXT)
    assert smtc.repeats == [RepeatMode.CONTEXT]
    states = []
    ctl.state_changed.connect(states.append)
    smtc.command_finished.emit("repeat", False, "Spotify отклонил смену повтора")
    assert states[-1].repeat is RepeatMode.OFF          # UI goes back to the real mode


def test_controller_refuses_repeat_without_capability(qtbot):
    smtc = RepeatSource()
    ctl = MediaController(smtc, None)
    smtc.push(track(repeat=None))
    ctl.set_repeat(RepeatMode.TRACK)
    assert smtc.repeats == []


def test_repeat_button_cycles(qtbot):
    win = make_window(qtbot)
    win.set_media_state(track(caps=REPEAT_CAPS, repeat=RepeatMode.OFF))
    modes = []
    win.repeat_requested.connect(modes.append)
    rect = win.view.progress_layout(win.view.capsule_rect(win.width()))["repeat"]
    assert rect is not None
    for _ in range(3):
        qtbot.mouseClick(win, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
    assert modes == [RepeatMode.CONTEXT, RepeatMode.TRACK, RepeatMode.OFF]
    assert win.view.state.repeat is RepeatMode.OFF


def test_no_repeat_button_when_player_does_not_report_it(qtbot):
    win = make_window(qtbot)
    assert win.view.progress_layout(win.view.capsule_rect(win.width()))["repeat"] is None
