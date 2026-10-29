import datetime as dt
import time

import pytest

from app.config import Settings
from app.island_widget import IslandView, format_time
from app.media.controller import MediaController
from app.media.cover_cache import CoverCache
from app.media.models import EMPTY_STATE, Capabilities, Command, MediaState, PlaybackStatus
from app.media.smtc import is_spotify, map_status, timeline_to_position
from tests.fakes import FakeApiSource, FakeSource

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x01" * 64
ALL_CAPS = Capabilities(True, True, True, True)


def track(title="Song", artist="Artist", status=PlaybackStatus.PLAYING, source="smtc", **kw):
    return MediaState(source=source, player_available=True, title=title, artist=artist, album="Album",
                      status=status, position_ms=kw.pop("position_ms", 1000), duration_ms=kw.pop("duration_ms", 200000),
                      caps=kw.pop("caps", ALL_CAPS), **kw)


# --------------------------------------------------------------------------- models
def test_position_extrapolates_only_while_playing():
    now = time.monotonic()
    playing = track(position_ms=10_000, position_timestamp=now - 2)
    paused = track(status=PlaybackStatus.PAUSED, position_ms=10_000, position_timestamp=now - 2)
    assert 11_900 <= playing.position_at(now) <= 12_100
    assert paused.position_at(now) == 10_000
    late = track(position_ms=199_000, duration_ms=200_000, position_timestamp=now - 60)
    assert late.position_at(now) == 200_000                        # clamped to duration


def test_same_as_ignores_small_drift():
    now = time.monotonic()
    a = track(position_ms=10_000, position_timestamp=now)
    assert a.same_as(track(position_ms=10_800, position_timestamp=now))
    assert not a.same_as(track(position_ms=20_000, position_timestamp=now))
    assert not a.same_as(track(title="Other", position_ms=10_000, position_timestamp=now))
    assert not a.same_as(None)


def test_caps():
    caps = Capabilities(play=True, pause=False, next=True, previous=False)
    assert caps.allows(Command.TOGGLE) and caps.allows(Command.NEXT)
    assert not caps.allows(Command.PREVIOUS) and not caps.allows(Command.PAUSE)


# --------------------------------------------------------------------------- SMTC helpers
def test_spotify_filter():
    assert is_spotify("Spotify.exe")
    assert is_spotify("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify")
    assert not is_spotify("Chrome")
    assert not is_spotify("")


def test_status_mapping():
    assert map_status(4) is PlaybackStatus.PLAYING
    assert map_status(5) is PlaybackStatus.PAUSED
    assert map_status(0) is PlaybackStatus.STOPPED
    assert map_status("junk") is PlaybackStatus.STOPPED


def test_timeline_conversion():
    now = dt.datetime(2026, 1, 1, 12, 0, 10, tzinfo=dt.timezone.utc)
    updated = now - dt.timedelta(seconds=10)
    pos, dur = timeline_to_position(dt.timedelta(seconds=30), dt.timedelta(seconds=120), updated, True, now)
    assert dur == 120_000 and pos == 40_000
    pos, _ = timeline_to_position(dt.timedelta(seconds=30), dt.timedelta(seconds=120), updated, False, now)
    assert pos == 30_000
    assert timeline_to_position(dt.timedelta(0), dt.timedelta(0), updated, True, now) == (None, None)
    # never-updated timeline (year 1601) must not extrapolate
    ancient = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
    assert timeline_to_position(dt.timedelta(seconds=5), dt.timedelta(seconds=60), ancient, True, now)[0] == 5_000


# --------------------------------------------------------------------------- cover cache
def test_cover_cache_dedupes_bytes(tmp_path):
    cache = CoverCache(tmp_path)
    p1 = cache.store_bytes(PNG)
    mtime = p1.stat().st_mtime_ns
    p2 = cache.store_bytes(PNG)
    assert p1 == p2 and p1.suffix == ".png"
    assert len(list(tmp_path.iterdir())) == 1
    assert p2.stat().st_mtime_ns >= mtime


def test_cover_cache_rejects_garbage(tmp_path):
    cache = CoverCache(tmp_path)
    assert cache.store_bytes(b"<html>not an image</html>") is None
    assert cache.store_bytes(b"") is None
    assert list(tmp_path.iterdir()) == []


def test_cover_cache_by_url(tmp_path):
    cache = CoverCache(tmp_path)
    url = "https://i.scdn.co/image/abc"
    assert cache.path_for_url(url) is None
    stored = cache.store_url(url, JPG)
    assert stored.suffix == ".jpg"
    assert cache.path_for_url(url) == stored            # second lookup -> no download needed


def test_cover_cache_prunes(tmp_path):
    cache = CoverCache(tmp_path, max_files=3)
    for i in range(6):
        cache.store_bytes(PNG + bytes([i]))
    cache.prune()
    assert len(list(tmp_path.iterdir())) == 3


# --------------------------------------------------------------------------- controller
def test_controller_prefers_smtc_and_routes_commands(qtbot):
    smtc, api = FakeSource("smtc"), FakeApiSource()
    ctl = MediaController(smtc, api)
    states = []
    ctl.state_changed.connect(states.append)
    smtc.push(track("A"))
    assert states[-1].title == "A" and ctl.active_source is smtc
    ctl.send_command(Command.NEXT)
    assert smtc.commands == [Command.NEXT] and api.commands == []


def test_controller_metadata_updates_and_pause(qtbot):
    smtc = FakeSource("smtc")
    ctl = MediaController(smtc, None)
    states = []
    ctl.state_changed.connect(states.append)
    smtc.push(track("A"))
    smtc.push(track("A"))                                    # identical -> no extra emission
    smtc.push(track("A", status=PlaybackStatus.PAUSED))
    smtc.push(track("B"))
    assert [(s.title, s.status) for s in states] == [
        ("A", PlaybackStatus.PLAYING), ("A", PlaybackStatus.PAUSED), ("B", PlaybackStatus.PLAYING)]


def test_controller_spotify_missing(qtbot):
    smtc = FakeSource("smtc")
    ctl = MediaController(smtc, None)
    notices = []
    ctl.notice.connect(notices.append)
    smtc.push(MediaState(source="smtc", player_available=False))
    assert ctl.state == EMPTY_STATE and ctl.active_source is None
    ctl.send_command(Command.TOGGLE)                         # must not crash
    assert notices == ["Spotify не запущен"]


def test_controller_falls_back_to_webapi(qtbot):
    smtc, api = FakeSource("smtc"), FakeApiSource()
    ctl = MediaController(smtc, api)
    api.connected = True
    smtc.push(MediaState(source="smtc", player_available=False))
    assert api.active_calls[-1] is True                      # poll the API only when SMTC has nothing
    api.push(track("Phone song", source="webapi"))
    assert ctl.state.title == "Phone song" and ctl.active_source is api
    ctl.send_command(Command.PAUSE)
    assert api.commands == [Command.PAUSE]
    smtc.push(track("Local"))                                # Spotify desktop appears again
    assert ctl.state.title == "Local" and api.active_calls[-1] is False


def test_controller_blocks_unavailable_command(qtbot):
    smtc = FakeSource("smtc")
    ctl = MediaController(smtc, None)
    notices = []
    ctl.notice.connect(notices.append)
    smtc.push(track(caps=Capabilities(play=True, pause=True, next=False, previous=False)))
    ctl.send_command(Command.NEXT)
    assert smtc.commands == [] and notices


def test_controller_reports_failed_command(qtbot):
    smtc = FakeSource("smtc")
    ctl = MediaController(smtc, None)
    notices = []
    ctl.notice.connect(notices.append)
    smtc.command_finished.emit("next", False, "Spotify отклонил команду")
    assert notices == ["Spotify отклонил команду"]


# --------------------------------------------------------------------------- view
def test_view_track_change_crossfades_and_releases_previous(qtbot, tmp_path):
    view = IslandView(Settings())
    view.set_state(track("A"))
    qtbot.waitUntil(lambda: view.prev_state is None, timeout=1500)
    view.set_state(track("B"))
    assert view.prev_state is not None and view.prev_state.title == "A"
    qtbot.waitUntil(lambda: view.prev_state is None, timeout=1500)


def test_view_pixmap_cache_is_bounded(qtbot, tmp_path):
    from PySide6.QtGui import QColor, QImage

    view = IslandView(Settings())
    for i in range(10):
        path = tmp_path / f"{i}.png"
        img = QImage(8, 8, QImage.Format.Format_RGB32)
        img.fill(QColor(i * 20, 10, 10))
        img.save(str(path))
        view.set_state(track(f"T{i}", cover_path=str(path)))
    assert view.cached_pixmaps <= 4                          # no growth across track changes


def test_view_buttons_hit_test(qtbot):
    view = IslandView(Settings())
    view.set_state(track())
    width = view.window_size().width()
    rects = view.button_rects(view.capsule_rect(width))
    assert view.button_at(rects[Command.NEXT].center(), width) is None      # collapsed: no buttons
    view.expansion.jump(1.0)
    rects = view.button_rects(view.capsule_rect(width))
    for command, rect in rects.items():
        assert view.button_at(rect.center(), width) is command


def test_view_timers_idle_when_paused_or_inactive(qtbot):
    view = IslandView(Settings())
    view.set_state(track())
    assert view.timers_running
    view.set_active(False)
    assert not view.timers_running
    view.set_active(True)
    view.set_state(track(status=PlaybackStatus.PAUSED))
    qtbot.waitUntil(lambda: not view.timers_running, timeout=1500)


@pytest.mark.parametrize("ms,text", [(0, "0:00"), (61_000, "1:01"), (3_723_000, "1:02:03"), (None, "–:––")])
def test_format_time(ms, text):
    assert format_time(ms) == text
