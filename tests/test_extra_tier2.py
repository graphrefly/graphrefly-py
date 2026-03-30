"""Tier 2 extra operators (roadmap §2.2) — dynamic maps, timers, buffers."""

from __future__ import annotations

import time
from typing import Any

from graphrefly.core import Messages, MessageType, node, state
from graphrefly.core.sugar import pipe
from graphrefly.extra.tier2 import (
    audit,
    buffer,
    buffer_count,
    buffer_time,
    concat_map,
    debounce,
    delay,
    exhaust_map,
    flat_map,
    gate,
    interval,
    pausable,
    repeat,
    rescue,
    sample,
    switch_map,
    throttle,
    timeout,
)


def _values(sink: list[Messages]) -> list[Any]:
    out: list[Any] = []
    for batch in sink:
        for m in batch:
            if m[0] is MessageType.DATA:
                out.append(m[1])
    return out


def _has_complete(sink: list[Messages]) -> bool:
    return any(m[0] is MessageType.COMPLETE for batch in sink for m in batch)


def _has_error(sink: list[Messages]) -> bool:
    return any(m[0] is MessageType.ERROR for batch in sink for m in batch)


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
    outer = state(0)
    p1, h1 = _make_deferred_producer()
    p2, h2 = _make_deferred_producer()
    p3, h3 = _make_deferred_producer()
    counter = [0]

    def make_inner(_v: int) -> Any:
        counter[0] += 1
        # counter 1 = initial dep value, 2 = DATA 1, 3 = DATA 2
        return [p1, p2, p3][counter[0] - 1]

    sink: list[Messages] = []
    out = pipe(outer, flat_map(make_inner))
    out.subscribe(sink.append)

    outer.down([(MessageType.DATA, 1)])
    outer.down([(MessageType.DATA, 2)])
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
    s = state(0)
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
    inp = state(0)
    tick = state(0)
    sink: list[Messages] = []
    sampled = pipe(inp, sample(tick))
    sampled.subscribe(sink.append)
    inp.down([(MessageType.DATA, 10)])
    inp.down([(MessageType.DATA, 20)])
    tick.down([(MessageType.DATA, 1)])
    assert _values(sink) == [20]


def test_buffer_notifer() -> None:
    src = state(0)
    gate = state(0)
    sink: list[Messages] = []
    b = pipe(src, buffer(gate))
    b.subscribe(sink.append)
    src.down([(MessageType.DATA, 1)])
    src.down([(MessageType.DATA, 2)])
    gate.down([(MessageType.DATA, 1)])
    assert _values(sink) == [[1, 2]]


def test_buffer_count() -> None:
    s = state(0)
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


def test_gate() -> None:
    """gate(control) forwards DATA when control is truthy; RESOLVED otherwise."""
    src = state(1)
    ctrl = state(True)
    sink: list[Messages] = []
    out = pipe(src, gate(ctrl))
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
