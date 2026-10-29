from app.animations import AnimatedValue, HoverIntent, smoothstep
from app.config import Settings
from app.island_widget import IslandView


def test_expand_and_delayed_collapse(qtbot):
    hover = HoverIntent(collapse_delay_ms=150)
    events = []
    hover.expand_requested.connect(lambda: events.append("expand"))
    hover.collapse_requested.connect(lambda: events.append("collapse"))

    hover.pointer_entered()
    assert events == ["expand"] and hover.expanded
    hover.pointer_entered()                          # repeated enter does not re-expand
    assert events == ["expand"]

    hover.pointer_left()
    assert hover.collapse_pending and hover.expanded
    qtbot.wait(60)
    assert events == ["expand"]                      # still within the delay
    qtbot.waitUntil(lambda: events == ["expand", "collapse"], timeout=1000)
    assert not hover.expanded


def test_reenter_cancels_collapse(qtbot):
    hover = HoverIntent(collapse_delay_ms=120)
    events = []
    hover.collapse_requested.connect(lambda: events.append("collapse"))
    hover.pointer_entered()
    hover.pointer_left()
    qtbot.wait(50)
    hover.pointer_entered()                          # came back before the delay ran out
    assert not hover.collapse_pending
    qtbot.wait(250)
    assert events == [] and hover.expanded


def test_force_collapse(qtbot):
    hover = HoverIntent(collapse_delay_ms=5000)
    hover.pointer_entered()
    hover.force_collapse()
    assert not hover.expanded and not hover.collapse_pending


def test_animated_value_retargets_from_current_value(qtbot):
    value = AnimatedValue(0.0, duration_ms=200)
    value.animate_to(1.0)
    qtbot.wait(80)
    mid = value.value
    assert 0.0 < mid < 1.0
    value.animate_to(0.0)                            # reverse mid-flight: no jump back to 1
    assert abs(value.value - mid) < 0.15
    qtbot.waitUntil(lambda: not value.running, timeout=1000)
    assert value.value == 0.0


def test_view_expansion_reverses_during_collapse(qtbot):
    view = IslandView(Settings())
    view.set_expanded(True)
    qtbot.waitUntil(lambda: not view.expansion.running, timeout=1500)
    assert view.expansion.value == 1.0
    view.set_expanded(False)
    qtbot.wait(100)
    partially = view.expansion.value
    assert 0.0 < partially < 1.0
    view.set_expanded(True)
    qtbot.waitUntil(lambda: not view.expansion.running, timeout=1500)
    assert view.expansion.value == 1.0


def test_capsule_grows_with_expansion():
    s = Settings(compact_width=180, expanded_width=440)
    view = IslandView(s)
    width = view.window_size().width()
    compact = view.capsule_rect(width)
    view.expansion.jump(1.0)
    expanded = view.capsule_rect(width)
    assert compact.width() == 180 and expanded.width() == 440
    assert abs(compact.center().x() - expanded.center().x()) < 0.01     # stays centred
    assert expanded.height() > compact.height()
    assert view.window_size().width() >= 440                            # room for overshoot


def test_smoothstep_bounds():
    assert smoothstep(0, 1, -1) == 0.0
    assert smoothstep(0, 1, 2) == 1.0
    assert 0.0 < smoothstep(0, 1, 0.5) < 1.0
