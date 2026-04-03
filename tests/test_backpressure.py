"""Backpressure: create_watermark_controller and GraphObserveSource.up()."""

from __future__ import annotations

from graphrefly.core import derived, state
from graphrefly.core.protocol import MessageType
from graphrefly.extra.backpressure import (
    WatermarkOptions,
    create_watermark_controller,
)
from graphrefly.graph.graph import Graph

# Short alias for readability in tests.
_wm = create_watermark_controller
_opts = WatermarkOptions

# ---------------------------------------------------------------------------
# WatermarkController unit tests
# ---------------------------------------------------------------------------


def test_pending_count_tracks():
    sent: list = []
    wm = _wm(sent.append, _opts(high_water_mark=5, low_water_mark=2))
    assert wm.pending == 0
    wm.on_enqueue()
    assert wm.pending == 1
    wm.on_enqueue()
    assert wm.pending == 2
    wm.on_dequeue()
    assert wm.pending == 1


def test_pause_at_high_watermark():
    sent: list = []
    wm = _wm(sent.append, _opts(high_water_mark=3, low_water_mark=1))
    wm.on_enqueue()
    wm.on_enqueue()
    assert not wm.paused
    assert len(sent) == 0

    paused = wm.on_enqueue()  # 3 = high_water_mark
    assert paused is True
    assert wm.paused is True
    assert len(sent) == 1
    assert sent[0][0][0] is MessageType.PAUSE


def test_resume_at_low_watermark():
    sent: list = []
    wm = _wm(sent.append, _opts(high_water_mark=3, low_water_mark=1))
    for _ in range(3):
        wm.on_enqueue()
    assert wm.paused

    wm.on_dequeue()  # 2 — still above low
    assert wm.paused
    assert len(sent) == 1

    resumed = wm.on_dequeue()  # 1 = low_water_mark
    assert resumed is True
    assert not wm.paused
    assert len(sent) == 2
    assert sent[1][0][0] is MessageType.RESUME


def test_no_resume_when_not_paused():
    sent: list = []
    wm = _wm(sent.append, _opts(high_water_mark=3, low_water_mark=1))
    wm.on_enqueue()
    wm.on_dequeue()
    assert len(sent) == 0


def test_no_duplicate_pause():
    sent: list = []
    wm = _wm(sent.append, _opts(high_water_mark=3, low_water_mark=1))
    for _ in range(5):
        wm.on_enqueue()
    assert len(sent) == 1  # only one PAUSE


def test_dispose_resumes_if_paused():
    sent: list = []
    wm = _wm(sent.append, _opts(high_water_mark=2, low_water_mark=0))
    wm.on_enqueue()
    wm.on_enqueue()
    assert wm.paused
    wm.dispose()
    assert not wm.paused
    assert len(sent) == 2
    assert sent[1][0][0] is MessageType.RESUME


def test_dispose_noop_when_not_paused():
    sent: list = []
    wm = _wm(sent.append, _opts(high_water_mark=3, low_water_mark=1))
    wm.dispose()
    assert len(sent) == 0


def test_unique_lock_ids():
    sent1: list = []
    sent2: list = []
    wm1 = _wm(sent1.append, _opts(high_water_mark=1, low_water_mark=0))
    wm2 = _wm(sent2.append, _opts(high_water_mark=1, low_water_mark=0))
    wm1.on_enqueue()
    wm2.on_enqueue()
    lock1 = sent1[0][0][1]
    lock2 = sent2[0][0][1]
    assert lock1 is not lock2  # object() identity — unforgeable, like TS Symbol


def test_pending_never_negative():
    sent: list = []
    wm = _wm(sent.append, _opts(high_water_mark=3, low_water_mark=1))
    wm.on_dequeue()
    wm.on_dequeue()
    assert wm.pending == 0


def test_invalid_watermark_options():
    import pytest

    with pytest.raises(ValueError, match="high_water_mark must be >= 1"):
        _wm(lambda _: None, _opts(high_water_mark=0, low_water_mark=0))
    with pytest.raises(ValueError, match="low_water_mark must be >= 0"):
        _wm(lambda _: None, _opts(high_water_mark=3, low_water_mark=-1))
    with pytest.raises(ValueError, match="low_water_mark must be < high_water_mark"):
        _wm(lambda _: None, _opts(high_water_mark=3, low_water_mark=3))
    with pytest.raises(ValueError, match="low_water_mark must be < high_water_mark"):
        _wm(lambda _: None, _opts(high_water_mark=3, low_water_mark=5))


def test_messages_are_lists():
    """Messages sent by WatermarkController are lists (not tuples) matching Messages type."""
    sent: list = []
    wm = _wm(sent.append, _opts(high_water_mark=1, low_water_mark=0))
    wm.on_enqueue()  # sends PAUSE
    assert isinstance(sent[0], list), f"Expected list, got {type(sent[0])}"


# ---------------------------------------------------------------------------
# GraphObserveSource.up() integration
# ---------------------------------------------------------------------------


def test_observe_up_single_path():
    """up() on a single-node observe propagates upstream through the node."""
    s = state(0)
    # derived depends on s — calling d.up() should reach s
    d = derived([s], lambda deps: deps[0])
    g = Graph("bp-up")
    g.add("s", s)
    g.add("d", d)

    # PAUSE/RESUME are forwarded downstream by the protocol, so we can
    # detect upstream propagation by subscribing to 's' and seeing if it
    # receives the PAUSE (nodes forward PAUSE downstream to sinks).
    received: list = []
    s.subscribe(lambda msgs: received.extend(msgs))

    obs = g.observe("d")
    obs.subscribe(lambda _msgs: None)
    # up() should not raise — it delegates to d.up() which fans out to s
    obs.up(((MessageType.PAUSE, "test-lock"),))

    # PAUSE propagates upstream from d to s.  Source nodes (no deps) are
    # no-ops for up().  The key assertion: no exception raised.
    g.destroy()


def test_observe_up_graph_wide():
    """up(path=...) on graph-wide observe targets the correct node."""
    a = state(0)
    b = state(0)
    g = Graph("bp-up-all")
    g.add("a", a)
    g.add("b", b)

    obs = g.observe()
    obs.subscribe(lambda _path, _msgs: None)
    # Should not raise — targets "a" specifically
    obs.up(((MessageType.PAUSE, "lock-a"),), path="a")

    # Calling up without path on graph-wide observe should error
    try:
        obs.up(((MessageType.PAUSE, "no-path"),))
        raised = False
    except ValueError:
        raised = True
    assert raised, "up() without path on graph-wide observe should raise ValueError"
    g.destroy()


def test_observe_up_reaches_deps():
    """up() on a derived node propagates to its dependencies."""
    s = state(0)
    d = derived([s], lambda deps: deps[0])
    g = Graph("bp-up-deps")
    g.add("s", s)
    g.add("d", d)

    # Use mock to spy on s.up via the internal _up method
    original_up = type(s).up
    call_log: list = []

    def patched_up(self_node, messages, **kw):
        call_log.append((self_node.name, messages))
        return original_up(self_node, messages, **kw)

    # Monkey-patch the class method temporarily
    type(s).up = patched_up
    try:
        obs = g.observe("d")
        obs.subscribe(lambda _msgs: None)
        obs.up(((MessageType.PAUSE, "lock"),))
        # d.up() should have called s.up()
        assert any(name == "s" for name, _ in call_log)
    finally:
        type(s).up = original_up
    g.destroy()


# ---------------------------------------------------------------------------
# GraphObserveSource.up() guard-denied silent drop
# ---------------------------------------------------------------------------


def test_observe_up_guard_denied_silently_dropped():
    """up() on a guarded node silently drops when guard denies (no raise)."""
    from graphrefly import policy

    # Allow observe (so subscribe works) but deny write (up defaults to "write")
    guard = policy(lambda allow, deny: [allow("observe"), deny("write")])
    s = state(0)
    d = derived([s], lambda deps: deps[0], guard=guard)

    g = Graph("bp-guard-drop")
    g.add("s", s)
    g.add("d", d)

    obs = g.observe("d")
    obs.subscribe(lambda _msgs: None)
    # Should NOT raise — GuardDenied is caught and silently dropped
    obs.up(((MessageType.PAUSE, "lock"),))
    g.destroy()


def test_observe_up_graph_wide_guard_denied_silently_dropped():
    """Graph-wide up(path=...) silently drops when guard denies."""
    from graphrefly import policy

    guard = policy(lambda allow, deny: [allow("observe"), deny("write")])
    n = state(0, guard=guard)

    g = Graph("bp-guard-drop-all")
    g.add("x", n)

    obs = g.observe()
    obs.subscribe(lambda _path, _msgs: None)
    # Should NOT raise
    obs.up(((MessageType.PAUSE, "lock"),), path="x")
    g.destroy()


def test_observe_up_non_guard_errors_still_raise():
    """up() re-raises non-GuardDenied exceptions (only GuardDenied is caught)."""
    import pytest

    s = state(0)
    g = Graph("bp-non-guard-err")
    g.add("s", s)

    # Graph-wide observe: path= resolves against registry, so nonexistent raises
    obs = g.observe()
    obs.subscribe(lambda _path, _msgs: None)
    with pytest.raises(KeyError):
        obs.up(((MessageType.PAUSE, "lock"),), path="nonexistent")
    g.destroy()


def test_watermark_controller_with_guarded_observe():
    """End-to-end: create_watermark_controller + guarded observe — silently dropped."""
    from graphrefly import policy

    guard = policy(lambda allow, deny: [allow("observe"), deny("write")])
    s = state(0)
    d = derived([s], lambda deps: deps[0], guard=guard)

    g = Graph("bp-e2e-guard")
    g.add("s", s)
    g.add("d", d)

    obs = g.observe("d")
    obs.subscribe(lambda _msgs: None)

    wm = _wm(obs.up, _opts(high_water_mark=2, low_water_mark=0))
    # Enqueue until PAUSE fires — should not raise
    wm.on_enqueue()
    wm.on_enqueue()
    assert wm.paused
    # Dispose sends RESUME — also should not raise
    wm.dispose()
    assert not wm.paused
    g.destroy()
