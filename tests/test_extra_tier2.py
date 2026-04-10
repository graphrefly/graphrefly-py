"""Tier 2 extra operators (roadmap §2.2) — dynamic maps, timers, buffers."""

from __future__ import annotations

import time
from typing import Any

from graphrefly.core import Messages, MessageType, node, state
from graphrefly.core.protocol import batch
from graphrefly.core.sugar import pipe
from graphrefly.extra.tier2 import (
    audit,
    buffer,
    buffer_count,
    buffer_time,
    catch_error,
    concat_map,
    debounce,
    debounce_time,
    delay,
    exhaust_map,
    flat_map,
    interval,
    merge_map,
    pausable,
    repeat,
    rescue,
    sample,
    switch_map,
    throttle,
    throttle_time,
    timeout,
    valve,
)


def _values(sink: list[Messages]) -> list[Any]:
    out: list[Any] = []
    for msgs in sink:
        for m in msgs:
            if m[0] is MessageType.DATA:
                out.append(m[1])
    return out


def _has_complete(sink: list[Messages]) -> bool:
    return any(m[0] is MessageType.COMPLETE for batch in sink for m in batch)


def _has_error(sink: list[Messages]) -> bool:
    return any(m[0] is MessageType.ERROR for batch in sink for m in batch)


def _flatten_types(sink: list[Messages]) -> list[MessageType]:
    return [m[0] for b in sink for m in b]


def _global_dirty_before_phase2(sink: list[Messages]) -> bool:
    flat = _flatten_types(sink)
    try:
        dirty_idx = flat.index(MessageType.DIRTY)
    except ValueError:
        return False
    phase2_idx = next(
        (i for i, t in enumerate(flat) if t in (MessageType.DATA, MessageType.RESOLVED)),
        -1,
    )
    return phase2_idx >= 0 and dirty_idx < phase2_idx


def _make_deferred_producer() -> tuple[Any, list[Any]]:
    holder: list[Any] = []

    def start(_d: list[Any], a: Any) -> Any:
        holder.append(a)
        return lambda: None

    p = node(start, describe_kind="deferred_producer")
    return p, holder


def test_switch_map_switches_inner() -> None:
    outer = state(0)
    inner1 = state("x")
    inner2 = state("y")
    inners = {0: state("_"), 1: inner1, 2: inner2}
    sink: list[Messages] = []
    out = pipe(outer, switch_map(lambda v: inners[v]))
    out.subscribe(sink.append)

    outer.down([(MessageType.DATA, 1)])
    inner1.down([(MessageType.DATA, "x1")])
    assert "x1" in _values(sink)

    outer.down([(MessageType.DATA, 2)])
    inner1.down([(MessageType.DATA, "x2")])
    inner2.down([(MessageType.DATA, "y1")])
    assert "x2" not in _values(sink)
    assert "y1" in _values(sink)


def test_switch_map_outer_error() -> None:
    p, holder = _make_deferred_producer()
    sink: list[Messages] = []
    out = pipe(p, switch_map(lambda _v: state(0)))
    out.subscribe(sink.append)
    holder[0].down([(MessageType.ERROR, ValueError("err"))])
    assert _has_error(sink)


def test_switch_map_initial() -> None:
    outer = state("a")
    out = pipe(outer, switch_map(lambda x: state(x + "!"), initial="init"))
    assert out.get() == "init"


def test_switch_map_coerces_scalar_project_return() -> None:
    outer, ho = _make_deferred_producer()
    sink: list[Messages] = []
    out = pipe(outer, switch_map(lambda v: (0 if v is None else v) + 100))
    out.subscribe(sink.append)
    ho[0].down([(MessageType.DATA, 2)])
    assert 102 in _values(sink)


def test_switch_map_coerces_awaitable_project_return() -> None:
    outer, ho = _make_deferred_producer()
    sink: list[Messages] = []

    async def plus5(v: int) -> int:
        return v + 5

    out = pipe(outer, switch_map(lambda v: plus5(v)))
    out.subscribe(sink.append)
    ho[0].down([(MessageType.DATA, 3)])
    time.sleep(0.1)
    assert 8 in _values(sink)


def test_concat_map_sequential() -> None:
    outer = state(0)
    results: list[tuple[int, list[Any]]] = []

    def make_inner(v: int) -> Any:
        _p, holder = _make_deferred_producer()
        results.append((v, holder))
        return _p

    sink: list[Messages] = []
    out = pipe(outer, concat_map(make_inner))
    out.subscribe(sink.append)

    # Initial value 0 is processed as a dep → results[0] = (0, ...)
    assert len(results) == 1
    assert results[0][0] == 0

    outer.down([(MessageType.DATA, 1)])
    outer.down([(MessageType.DATA, 2)])
    # 1 and 2 are queued since inner for 0 is still active
    _, h0 = results[0]
    h0[0].down([(MessageType.DATA, "a")])
    assert "a" in _values(sink)
    h0[0].down([(MessageType.COMPLETE,)])
    assert len(results) == 2
    assert results[1][0] == 1


def test_concat_map_max_buffer() -> None:
    outer = state(0)
    inners: list[tuple[int, list[Any]]] = []

    def make_inner(v: int) -> Any:
        _p, holder = _make_deferred_producer()
        inners.append((v, holder))
        return _p

    out = pipe(outer, concat_map(make_inner, max_buffer=1))
    out.subscribe(lambda _m: None)

    # Initial value 0 is processed as dep → inners[0] = (0, ...)
    outer.down([(MessageType.DATA, 1)])
    outer.down([(MessageType.DATA, 2)])
    outer.down([(MessageType.DATA, 3)])
    # max_buffer=1 keeps only latest queued: 3
    _, h0 = inners[0]
    h0[0].down([(MessageType.COMPLETE,)])
    assert len(inners) == 2
    assert inners[1][0] == 3


def test_concat_map_coerces_iterable_project_return() -> None:
    outer = state(4)
    sink: list[Messages] = []
    out = pipe(outer, concat_map(lambda v: [v * 2, v * 3]))
    out.subscribe(sink.append)
    vals = _values(sink)
    assert 8 in vals
    assert 12 in vals


def test_flat_map_concurrent() -> None:
    outer, ho = _make_deferred_producer()
    p_init, _h_init = _make_deferred_producer()  # absorbs initial compute
    p1, h1 = _make_deferred_producer()
    p2, h2 = _make_deferred_producer()
    counter = [0]

    def make_inner(_v: int) -> Any:
        counter[0] += 1
        # counter 1 = initial compute (absorbed), 2 = DATA 1, 3 = DATA 2
        return [p_init, p1, p2][counter[0] - 1]

    sink: list[Messages] = []
    out = pipe(outer, flat_map(make_inner))
    out.subscribe(sink.append)

    ho[0].down([(MessageType.DATA, 1)])
    ho[0].down([(MessageType.DATA, 2)])
    h1[0].down([(MessageType.DATA, "from-1")])
    h2[0].down([(MessageType.DATA, "from-2")])
    vals = _values(sink)
    assert "from-1" in vals
    assert "from-2" in vals


def test_flat_map_completes_when_all_done() -> None:
    op, ho = _make_deferred_producer()
    ip, hi = _make_deferred_producer()
    sink: list[Messages] = []
    out = pipe(op, flat_map(lambda _v: ip))
    out.subscribe(sink.append)
    ho[0].down([(MessageType.DATA, 1)])
    ho[0].down([(MessageType.COMPLETE,)])
    assert not _has_complete(sink)
    hi[0].down([(MessageType.COMPLETE,)])
    assert _has_complete(sink)


def test_flat_map_coerces_async_iterable_project_return() -> None:
    outer = state(10)
    sink: list[Messages] = []

    async def gen(v: int) -> object:
        yield v + 1
        yield v + 2

    out = pipe(outer, flat_map(lambda v: gen(v)))
    out.subscribe(sink.append)
    time.sleep(0.1)
    vals = _values(sink)
    assert 11 in vals
    assert 12 in vals


def test_exhaust_map_drops_while_busy() -> None:
    outer = state(0)
    p, h = _make_deferred_producer()
    sink: list[Messages] = []
    # v==0 (initial) and v==1 use deferred producer p; others use state("late")
    out = pipe(outer, exhaust_map(lambda v: p if v in (0, 1) else state("late")))
    out.subscribe(sink.append)

    # Initial value 0 subscribes to p (busy=True)
    outer.down([(MessageType.DATA, 1)])  # ignored — busy
    outer.down([(MessageType.DATA, 2)])  # ignored — busy
    h[0].down([(MessageType.DATA, "a")])
    assert "a" in _values(sink)
    h[0].down([(MessageType.COMPLETE,)])
    outer.down([(MessageType.DATA, 3)])
    assert "late" in _values(sink) or any("late" in str(x) for x in _values(sink))


def test_debounce_basic() -> None:
    s = state(0)
    sink: list[Messages] = []
    d = pipe(s, debounce(0.05))
    d.subscribe(sink.append)
    s.down([(MessageType.DATA, 1)])
    s.down([(MessageType.DATA, 2)])
    s.down([(MessageType.DATA, 3)])
    time.sleep(0.1)
    assert _values(sink) == [3]


def test_throttle_leading_edge() -> None:
    s = node()
    sink: list[Messages] = []
    t = pipe(s, throttle(0.1, trailing=False))
    t.subscribe(sink.append)
    s.down([(MessageType.DATA, 1)])
    s.down([(MessageType.DATA, 2)])
    s.down([(MessageType.DATA, 3)])
    assert _values(sink) == [1]
    time.sleep(0.15)
    s.down([(MessageType.DATA, 4)])
    assert _values(sink) == [1, 4]


def test_sample_on_notifier() -> None:
    inp = node()
    tick = node()
    sink: list[Messages] = []
    sampled = pipe(inp, sample(tick))
    sampled.subscribe(sink.append)
    inp.down([(MessageType.DATA, 10)])
    inp.down([(MessageType.DATA, 20)])
    tick.down([(MessageType.DATA, 1)])
    assert _values(sink) == [20]


def test_buffer_notifer() -> None:
    src = node()
    gate = node()
    sink: list[Messages] = []
    b = pipe(src, buffer(gate))
    b.subscribe(sink.append)
    src.down([(MessageType.DATA, 1)])
    src.down([(MessageType.DATA, 2)])
    gate.down([(MessageType.DATA, 1)])
    assert _values(sink) == [[1, 2]]


def test_buffer_count() -> None:
    s = node()
    sink: list[Messages] = []
    b = pipe(s, buffer_count(2))
    b.subscribe(sink.append)
    s.down([(MessageType.DATA, "a")])
    s.down([(MessageType.DATA, "b")])
    assert _values(sink) == [["a", "b"]]


def test_rescue() -> None:
    p, h = _make_deferred_producer()
    sink: list[Messages] = []
    out = pipe(p, rescue(lambda _e: "ok"))
    out.subscribe(sink.append)
    h[0].down([(MessageType.ERROR, RuntimeError("x"))])
    assert _values(sink) == ["ok"]


def test_rxjs_alias_identity_tier2() -> None:
    assert debounce_time is debounce
    assert throttle_time is throttle
    assert catch_error is rescue
    assert merge_map is flat_map


def test_valve() -> None:
    """valve(control) forwards DATA when control is truthy; RESOLVED otherwise."""
    src = state(1)
    ctrl = state(True)
    sink: list[Messages] = []
    out = pipe(src, valve(ctrl))
    out.subscribe(sink.append)
    assert out.get() == 1
    ctrl.down([(MessageType.DATA, False)])
    src.down([(MessageType.DIRTY,), (MessageType.DATA, 2)])
    assert MessageType.RESOLVED in {m[0] for batch in sink for m in batch}


def test_pausable_protocol() -> None:
    """pausable() buffers DIRTY/DATA/RESOLVED during PAUSE; flushes on RESUME."""
    src = state(0)
    sink: list[Messages] = []
    out = pipe(src, pausable())
    out.subscribe(sink.append)
    # Pause
    src.down([(MessageType.PAUSE,)])
    sink.clear()
    src.down([(MessageType.DIRTY,), (MessageType.DATA, 42)])
    # While paused, no DATA 42 should reach sink yet
    data_during_pause = [
        m for batch in sink for m in batch if m[0] is MessageType.DATA and len(m) > 1 and m[1] == 42
    ]
    assert data_during_pause == [], "DATA should be buffered during PAUSE"
    # Resume
    src.down([(MessageType.RESUME,)])
    data_after_resume = [
        m for batch in sink for m in batch if m[0] is MessageType.DATA and len(m) > 1 and m[1] == 42
    ]
    assert len(data_after_resume) > 0, "DATA should flush on RESUME"


def test_timeout_fires() -> None:
    s = state(0)
    sink: list[Messages] = []
    out = pipe(s, timeout(0.05))
    out.subscribe(sink.append)
    time.sleep(0.12)
    assert _has_error(sink)


def test_interval_emits() -> None:
    sink: list[Messages] = []
    n = interval(0.05)
    u = n.subscribe(sink.append)
    time.sleep(0.18)
    u()
    vals = _values(sink)
    assert len(vals) >= 2
    assert vals[0] == 0
    assert vals[1] == 1


def test_delay_defers_data() -> None:
    src = state(0)
    sink: list[Messages] = []
    out = pipe(src, delay(0.05))
    out.subscribe(sink.append)
    src.down([(MessageType.DATA, 99)])
    # Immediately after, no DATA yet
    assert 99 not in _values(sink)
    time.sleep(0.12)
    assert 99 in _values(sink)


def test_audit_trailing_only() -> None:
    """audit should NOT emit on the leading edge (trailing-only)."""
    src = state(0)
    sink: list[Messages] = []
    out = pipe(src, audit(0.05))
    out.subscribe(sink.append)
    src.down([(MessageType.DATA, 1)])
    # Leading edge: no emit yet
    vals_immediate = _values(sink)
    assert 1 not in vals_immediate, "audit should not emit on leading edge"
    time.sleep(0.12)
    assert 1 in _values(sink), "audit should emit after timer fires"


def test_repeat_replays() -> None:
    src = state(10, resubscribable=True)
    sink: list[Messages] = []
    out = pipe(src, repeat(2))
    out.subscribe(sink.append)
    # Each subscription round should emit 10, then COMPLETE triggers re-sub
    src.down([(MessageType.COMPLETE,)])
    src.down([(MessageType.COMPLETE,)])
    # Should have completed after 2 rounds
    assert any(m[0] is MessageType.COMPLETE for batch in sink for m in batch)


def test_buffer_time_flushes() -> None:
    src = state(0)
    sink: list[Messages] = []
    out = pipe(src, buffer_time(0.05))
    out.subscribe(sink.append)
    src.down([(MessageType.DATA, 1)])
    src.down([(MessageType.DATA, 2)])
    time.sleep(0.12)
    vals = _values(sink)
    assert any(isinstance(v, list) and 1 in v and 2 in v for v in vals)


# ---------------------------------------------------------------------------
# Tier 2 teardown and reconnect freshness
# ---------------------------------------------------------------------------


def test_switch_map_reconnect_fresh_inner() -> None:
    """After teardown + resubscribe + new outer DATA, switch_map creates a fresh inner
    subscription with no state from the previous one.

    switch_map stores inner-subscription state in a closure. After teardown, the inner
    is cleared (via the cleanup returned from compute). A new outer DATA after reconnect
    must produce a new inner subscription, not reference the old one.
    """
    outer = state(0)
    inner_a = state("a")
    inner_b = state("b")
    call_count = [0]
    inners = [inner_a, inner_b]

    def project(_v: Any) -> Any:
        i = min(call_count[0], 1)
        call_count[0] += 1
        return inners[i]

    out = pipe(outer, switch_map(project))
    sink1: list[Messages] = []
    unsub1 = out.subscribe(sink1.append)
    # First inner (inner_a) is subscribed on initial compute
    inner_a.down([(MessageType.DATA, "first")])
    assert "first" in _values(sink1)

    # Teardown
    unsub1()
    out.unsubscribe()
    sink1.clear()

    # Reconnect
    sink2: list[Messages] = []
    unsub2 = out.subscribe(sink2.append)
    # A new DATA on outer triggers attach() → project() → inner_b is the new inner
    outer.down([(MessageType.DATA, 1)])
    inner_b.down([(MessageType.DATA, "second")])
    assert "second" in _values(sink2), "new inner subscription should receive fresh values"
    # Stale inner_a should NOT reach the new subscription
    inner_a.down([(MessageType.DATA, "stale")])
    assert "stale" not in _values(sink2), "stale inner should not reach new subscription"
    unsub2()


def test_debounce_teardown_cancels_timer() -> None:
    """After unsubscribe, advancing time does not produce stale emissions from debounce."""
    s = state(0)
    sink: list[Messages] = []
    out = pipe(s, debounce(0.1))
    unsub = out.subscribe(sink.append)
    s.down([(MessageType.DATA, 99)])
    # Unsubscribe before timer fires
    unsub()
    out.unsubscribe()
    # Wait longer than debounce window
    time.sleep(0.25)
    # No further data should appear (subscription was torn down)
    vals = _values(sink)
    assert 99 not in vals, "stale debounce emission after teardown: " + str(vals)


def test_concat_map_reconnect_fresh_queue() -> None:
    """After teardown + resubscribe, concat_map continues processing in queue order.

    concat_map uses a closure-level queue that is not erased on unsubscribe (the same
    node instance retains its state). After reconnect, any queued outer values from
    before teardown are processed before newly enqueued ones. This test verifies that
    reconnect does not cause a crash and that values flow correctly from existing queue
    state through the new subscription.
    """
    outer, ho = _make_deferred_producer()
    results: list[tuple[int, list[Any]]] = []

    def make_inner(v: Any) -> Any:
        _p, holder = _make_deferred_producer()
        results.append((v, holder))
        return _p

    out = pipe(outer, concat_map(make_inner))

    # First subscription: initial compute spawns an inner for None (SENTINEL dep).
    # Then we send DATA(0) which queues behind that initial inner.
    sink1: list[Messages] = []
    unsub1 = out.subscribe(sink1.append)
    # Complete the initial inner (spawned by compute with None dep value)
    assert len(results) >= 1
    _, h_init = results[0]
    h_init[0].down([(MessageType.COMPLETE,)])
    # Now send DATA(0) — it was buffered, now drains
    ho[0].down([(MessageType.DATA, 0)])
    inners_0 = [(v, h) for v, h in results if v == 0]
    assert inners_0, "inner for value 0 should be created"
    _, h0 = inners_0[-1]
    h0[0].down([(MessageType.DATA, "zero")])
    h0[0].down([(MessageType.COMPLETE,)])
    unsub1()
    out.unsubscribe()

    # Second subscription — reconnect then push a new value
    sink2: list[Messages] = []
    unsub2 = out.subscribe(sink2.append)
    # Reconnect triggers another initial compute inner — complete it
    init_inners = [(v, h) for v, h in results if v is None]
    if len(init_inners) > 1:
        _, h_init2 = init_inners[-1]
        h_init2[0].down([(MessageType.COMPLETE,)])
    ho[0].down([(MessageType.DATA, 42)])
    # After reconnect + new DATA, a new inner should be created for value 42
    inners_42 = [(v, h) for v, h in results if v == 42]
    assert inners_42, "concat_map should create a new inner for value 42 after reconnect"
    _, h42 = inners_42[-1]
    h42[0].down([(MessageType.DATA, "fresh")])
    assert "fresh" in _values(sink2), "reconnected concat_map should deliver value from new inner"
    h42[0].down([(MessageType.COMPLETE,)])
    unsub2()


def test_switch_map_derived_inner_initial_data_not_duplicated() -> None:
    """Regression: session parity fix #4 (_forward_inner emitted flag).

    With push-on-subscribe, each attach (subscribe to inner) emits exactly once.
    The deferred-producer outer triggers an initial compute (attach with None),
    then the explicit DATA triggers a second attach. Each attach subscribes to
    the derived inner which pushes its value once — verifying _forward_inner's
    emitted flag prevents double-emission per attach.
    """
    outer, ho = _make_deferred_producer()
    base = state(10)
    derived_inner = node([base], lambda d, _m: d[0] + 1)
    sink: list[Messages] = []
    out = pipe(outer, switch_map(lambda _v: derived_inner))
    out.subscribe(sink.append)
    # Initial compute attaches with None → derived_inner emits 11 once
    vals_before = _values(sink)
    assert vals_before.count(11) == 1, f"initial attach should emit 11 once, got {vals_before}"
    ho[0].down([(MessageType.DATA, 0)])
    # Second attach re-subscribes → derived_inner emits 11 once more
    vals_after = _values(sink)
    assert vals_after.count(11) == 2, f"after explicit DATA, expect 2 total, got {vals_after}"


def test_switch_map_global_dirty_before_phase2() -> None:
    outer, holder = _make_deferred_producer()
    inner = state(10)
    sink: list[Messages] = []
    out = pipe(outer, switch_map(lambda _v: inner))
    out.subscribe(sink.append)
    holder[0].down([(MessageType.DIRTY,), (MessageType.DATA, 1)])
    sink.clear()
    inner.down([(MessageType.DIRTY,), (MessageType.DATA, 99)])
    assert _global_dirty_before_phase2(sink)


def test_concat_map_global_dirty_before_phase2() -> None:
    outer, holder = _make_deferred_producer()
    inner = state(10)
    sink: list[Messages] = []
    out = pipe(outer, concat_map(lambda _v: inner))
    out.subscribe(sink.append)
    holder[0].down([(MessageType.DIRTY,), (MessageType.DATA, 1)])
    sink.clear()
    inner.down([(MessageType.DIRTY,), (MessageType.DATA, 77)])
    assert _global_dirty_before_phase2(sink)


def test_flat_map_global_dirty_before_phase2() -> None:
    outer, holder = _make_deferred_producer()
    inner = state(10)
    sink: list[Messages] = []
    out = pipe(outer, flat_map(lambda _v: inner))
    out.subscribe(sink.append)
    holder[0].down([(MessageType.DIRTY,), (MessageType.DATA, 1)])
    sink.clear()
    inner.down([(MessageType.DIRTY,), (MessageType.DATA, 55)])
    assert _global_dirty_before_phase2(sink)


def test_exhaust_map_global_dirty_before_phase2() -> None:
    outer, holder = _make_deferred_producer()
    inner = state(10)
    sink: list[Messages] = []
    out = pipe(outer, exhaust_map(lambda _v: inner))
    out.subscribe(sink.append)
    holder[0].down([(MessageType.DIRTY,), (MessageType.DATA, 1)])
    sink.clear()
    inner.down([(MessageType.DIRTY,), (MessageType.DATA, 66)])
    assert _global_dirty_before_phase2(sink)


def test_concat_map_void_inner_data_before_complete() -> None:
    outer = state(0)
    sink: list[Messages] = []

    def void_once() -> Any:
        return node(
            lambda _d, a: a.down([(MessageType.DATA, None), (MessageType.COMPLETE,)]),
            describe_kind="void_once",
        )

    out = pipe(outer, concat_map(lambda _v: void_once()))
    out.subscribe(sink.append)
    outer.down([(MessageType.DATA, 1)])
    types = _flatten_types(sink)
    assert MessageType.DATA in types
    if MessageType.COMPLETE in types:
        assert types.index(MessageType.DATA) < types.index(MessageType.COMPLETE)


def test_flat_map_void_inner_data_before_complete() -> None:
    outer = state(0)
    sink: list[Messages] = []

    def void_once() -> Any:
        return node(
            lambda _d, a: a.down([(MessageType.DATA, None), (MessageType.COMPLETE,)]),
            describe_kind="void_once",
        )

    out = pipe(outer, flat_map(lambda _v: void_once()))
    out.subscribe(sink.append)
    outer.down([(MessageType.DATA, 1)])
    types = _flatten_types(sink)
    assert MessageType.DATA in types
    if MessageType.COMPLETE in types:
        assert types.index(MessageType.DATA) < types.index(MessageType.COMPLETE)


def test_exhaust_map_void_inner_data_before_complete() -> None:
    outer = state(0)
    sink: list[Messages] = []

    def void_once() -> Any:
        return node(
            lambda _d, a: a.down([(MessageType.DATA, None), (MessageType.COMPLETE,)]),
            describe_kind="void_once",
        )

    out = pipe(outer, exhaust_map(lambda _v: void_once()))
    out.subscribe(sink.append)
    outer.down([(MessageType.DATA, 1)])
    types = _flatten_types(sink)
    assert MessageType.DATA in types
    if MessageType.COMPLETE in types:
        assert types.index(MessageType.DATA) < types.index(MessageType.COMPLETE)


def test_rescue_wraps_switch_map_inner_error() -> None:
    outer = state(0)
    sink: list[Messages] = []

    def failing_inner(_v: Any) -> Any:
        return node(
            lambda _d, a: a.down(
                [(MessageType.ERROR, ValueError("inner")), (MessageType.COMPLETE,)]
            ),
            describe_kind="failing_inner",
        )

    out = pipe(outer, switch_map(failing_inner), rescue(lambda _e: 123))
    out.subscribe(sink.append)
    outer.down([(MessageType.DATA, 1)])
    assert 123 in _values(sink)
    assert not _has_error(sink)


def test_debounce_does_not_fire_inside_batch() -> None:
    s = state(0)
    sink: list[Messages] = []
    out = pipe(s, debounce(0.03))
    out.subscribe(sink.append)
    with batch():
        s.down([(MessageType.DATA, 7)])
        time.sleep(0.06)
        assert 7 not in _values(sink)
    time.sleep(0.04)
    assert 7 in _values(sink)
