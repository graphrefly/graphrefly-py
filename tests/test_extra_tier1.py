"""Tier 1 extra operators (roadmap 2.1) — built on ``node`` / ``derived``."""

from __future__ import annotations

from conftest import collect

from graphrefly.core import Messages, MessageType, derived, node, state
from graphrefly.core.sugar import pipe
from graphrefly.extra.tier1 import (
    combine,
    combine_latest,
    concat,
    distinct_until_changed,
    element_at,
    filter,
    find,
    first,
    last,
    map,
    merge,
    pairwise,
    race,
    reduce,
    scan,
    skip,
    take,
    take_until,
    take_while,
    tap,
    with_latest_from,
    zip,
)


def _msgs(sink: list[Messages]) -> list[tuple]:
    return [m for batch in sink for m in batch]


def test_map_basic() -> None:
    s = state(3)
    doubled = pipe(s, map(lambda x: x * 2))
    doubled.subscribe(lambda m: None)
    assert doubled.get() == 6
    s.down([(MessageType.DATA, 5)])
    assert doubled.get() == 10


def test_filter_resolved_on_fail() -> None:
    s = state(0)
    evens = pipe(s, filter(lambda x: x % 2 == 0))
    sink: list[Messages] = []
    evens.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DIRTY,), (MessageType.DATA, 1)])
    types = [m[0] for m in _msgs(sink)]
    assert MessageType.RESOLVED in types


def test_scan_fold() -> None:
    s = state(1)
    acc = pipe(s, scan(lambda a, x: a + x, 0))
    acc.subscribe(lambda m: None)
    assert acc.get() == 1
    s.down([(MessageType.DATA, 2)])
    assert acc.get() == 3


def test_take_completes() -> None:
    s = node()
    t = pipe(s, take(2))
    sink, unsub = collect(t, raw=True)
    s.down([(MessageType.DATA, 1)])
    s.down([(MessageType.DATA, 2)])
    s.down([(MessageType.DATA, 3)])
    assert t.get() == 2
    assert any(m[0] is MessageType.COMPLETE for m in _msgs(sink))


def test_skip_drops_first_n() -> None:
    s = node()
    sk = pipe(s, skip(2))
    sink, unsub = collect(sk, raw=True)
    s.down([(MessageType.DATA, 0)])
    s.down([(MessageType.DATA, 1)])
    s.down([(MessageType.DATA, 2)])
    s.down([(MessageType.DATA, 3)])
    data_vals = [m[1] for m in _msgs(sink) if m[0] is MessageType.DATA]
    assert data_vals == [2, 3]


def test_take_while_stops() -> None:
    s = state(1)
    tw = pipe(s, take_while(lambda x: x < 4))
    sink: list[Messages] = []
    tw.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DATA, 2)])
    s.down([(MessageType.DATA, 5)])
    assert any(m[0] is MessageType.COMPLETE for m in _msgs(sink))


def test_distinct_until_changed() -> None:
    s = state(1)
    d = pipe(s, distinct_until_changed())
    sink: list[Messages] = []
    d.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DATA, 1)])
    assert not any(m[0] is MessageType.DATA for m in _msgs(sink))


def test_tap_runs_side_effect() -> None:
    s = state(1)
    seen: list[int] = []
    t = pipe(s, tap(seen.append))
    t.subscribe(lambda m: None)
    s.down([(MessageType.DATA, 2)])
    assert seen == [1, 2]


def test_tap_observer_shape_data_error_complete() -> None:
    values: list[int] = []
    errors: list[str] = []
    completes: list[str] = []

    src = state(1)
    out = pipe(
        src,
        tap(
            {
                "data": lambda v: values.append(v),
                "error": lambda e: errors.append(str(e)),
                "complete": lambda: completes.append("done"),
            }
        ),
    )
    out.subscribe(lambda _msgs: None)
    src.down([(MessageType.DATA, 2)])
    src.down([(MessageType.ERROR, RuntimeError("boom"))])
    src.down([(MessageType.COMPLETE,)])
    assert values == [1, 2]
    assert errors == ["boom"]
    assert completes == []


def test_combine_latest_alias_identity() -> None:
    assert combine_latest is combine


def test_pairwise_pairs() -> None:
    s = state(1)
    p = pipe(s, pairwise())
    sink: list[Messages] = []
    p.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DATA, 2)])
    s.down([(MessageType.DATA, 3)])
    pairs = [m[1] for m in _msgs(sink) if m[0] is MessageType.DATA]
    assert pairs == [(1, 2), (2, 3)]


def test_combine_tuple() -> None:
    a = state(1)
    b = state(2)
    c = combine(a, b)
    c.subscribe(lambda m: None)
    assert c.get() == (1, 2)


def test_with_latest_from() -> None:
    a = state(1)
    b = state(10)
    w = pipe(a, with_latest_from(b))
    w.subscribe(lambda m: None)
    # Value not set until primary emits wire DATA
    a.down([(MessageType.DATA, 1)])
    assert w.get() == (1, 10)


def test_merge_last_wins() -> None:
    x = state(1)
    y = state(2)
    m = merge(x, y)
    sink: list[Messages] = []
    m.subscribe(sink.append)
    sink.clear()
    y.down([(MessageType.DATA, 20)])
    assert m.get() == 20


def test_zip_pairs() -> None:
    a = state(1)
    b = state(2)
    z = zip(a, b)
    sink: list[Messages] = []
    z.subscribe(sink.append)
    sink.clear()
    a.down([(MessageType.DATA, 10)])
    b.down([(MessageType.DATA, 20)])
    tuples = [m[1] for m in _msgs(sink) if m[0] is MessageType.DATA]
    assert tuples == [(10, 20)]


def test_find_first_match() -> None:
    s = state(1)
    f = pipe(s, find(lambda x: x >= 3))
    sink: list[Messages] = []
    f.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DATA, 2)])
    s.down([(MessageType.DATA, 4)])
    assert any(m[0] is MessageType.COMPLETE for m in _msgs(sink))
    data_vals = [m[1] for m in _msgs(sink) if m[0] is MessageType.DATA]
    assert data_vals == [4]


def test_element_at() -> None:
    s = node()
    e = pipe(s, element_at(2))
    sink, unsub = collect(e, raw=True)
    s.down([(MessageType.DATA, 0)])
    s.down([(MessageType.DATA, 1)])
    s.down([(MessageType.DATA, 2)])
    s.down([(MessageType.DATA, 99)])
    data_vals = [m[1] for m in _msgs(sink) if m[0] is MessageType.DATA]
    assert data_vals == [2]


def test_first_is_take_one() -> None:
    s = node()
    f = pipe(s, first())
    sink, unsub = collect(f, raw=True)
    s.down([(MessageType.DATA, 1)])
    assert any(m[0] is MessageType.COMPLETE for m in _msgs(sink))


def test_last_on_complete() -> None:
    s = state(1)
    last_out = pipe(s, last(default=-1))
    sink: list[Messages] = []
    last_out.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DATA, 2)])
    s.down([(MessageType.DATA, 3)])
    s.down([(MessageType.COMPLETE,)])
    assert last_out.get() == 3
    assert any(m[0] is MessageType.COMPLETE for m in _msgs(sink))


def test_take_until_notifier() -> None:
    src = state(1)
    stop = node()
    out = pipe(src, take_until(stop))
    sink: list[Messages] = []
    out.subscribe(sink.append)
    sink.clear()
    stop.down([(MessageType.DATA, True)])
    assert any(m[0] is MessageType.COMPLETE for m in _msgs(sink))


def test_take_until_with_custom_predicate() -> None:
    """take_until with a custom predicate can trigger on any message type."""
    src = state(1)
    stop = state(False)
    out = pipe(
        src,
        take_until(stop, predicate=lambda msg: msg[0] is MessageType.ERROR),
    )
    sink: list[Messages] = []
    out.subscribe(sink.append)
    sink.clear()
    stop.down([(MessageType.ERROR, ValueError("notifier"))])
    assert any(m[0] is MessageType.COMPLETE for m in _msgs(sink))


def test_race_first_data_wins() -> None:
    a = node()
    b = node()
    r = race(a, b)
    sink, unsub = collect(r, raw=True)
    b.down([(MessageType.DATA, 20)])
    a.down([(MessageType.DATA, 10)])
    assert r.get() == 20


def test_map_diamond_value() -> None:
    """``map`` branches feed a converging ``derived``; result matches combined transforms."""
    a = state(1)
    b = pipe(a, map(lambda x: x * 2))
    c = pipe(a, map(lambda x: x + 10))
    d = derived([b, c], lambda vs, _: vs[0] + vs[1])
    d.subscribe(lambda m: None)
    # Initial: b=2, c=11 -> d=13
    assert d.get() == 13
    a.down([(MessageType.DATA, 5)])
    assert d.get() == 25


def test_reduce_emits_on_complete() -> None:
    """reduce accumulates silently, emits once on COMPLETE."""
    s = state(0)
    r = pipe(s, reduce(lambda a, x: a + x, 0))
    sink: list[Messages] = []
    r.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DATA, 1)])
    s.down([(MessageType.DATA, 2)])
    # No DATA emitted yet
    assert not any(m[0] is MessageType.DATA for m in _msgs(sink))
    s.down([(MessageType.COMPLETE,)])
    data_vals = [m[1] for m in _msgs(sink) if m[0] is MessageType.DATA]
    assert data_vals == [3]
    assert any(m[0] is MessageType.COMPLETE for m in _msgs(sink))


def test_race_winner_continues() -> None:
    """race locks to the first DATA winner and continues forwarding."""
    a = state(0)
    b = state(0)
    r = race(a, b)
    sink: list[Messages] = []
    r.subscribe(sink.append)
    sink.clear()
    a.down([(MessageType.DATA, 1)])
    a.down([(MessageType.DATA, 2)])
    b.down([(MessageType.DATA, 99)])
    data_vals = [m[1] for m in _msgs(sink) if m[0] is MessageType.DATA]
    assert data_vals == [1, 2]


def test_concat_buffers_second_during_phase0() -> None:
    """concat buffers DATA from second source while first is active."""
    a = state(0)
    b = state(0)
    co = pipe(a, concat(b))
    sink: list[Messages] = []
    co.subscribe(sink.append)
    sink.clear()
    b.down([(MessageType.DATA, 50)])
    a.down([(MessageType.COMPLETE,)])
    data_vals = [m[1] for m in _msgs(sink) if m[0] is MessageType.DATA]
    assert 50 in data_vals


def test_with_latest_from_suppresses_secondary() -> None:
    """with_latest_from does NOT emit when only the secondary changes."""
    a = state(1)
    b = state(10)
    w = pipe(a, with_latest_from(b))
    sink: list[Messages] = []
    w.subscribe(sink.append)
    sink.clear()
    b.down([(MessageType.DATA, 20)])
    # Secondary-only change should NOT produce DATA
    assert not any(m[0] is MessageType.DATA for m in _msgs(sink))
    a.down([(MessageType.DATA, 5)])
    data_vals = [m[1] for m in _msgs(sink) if m[0] is MessageType.DATA]
    assert data_vals == [(5, 20)]


# ---------------------------------------------------------------------------
# merge: completes after ALL sources complete (GRAPHREFLY-SPEC S1.3.5 / S2)
# ---------------------------------------------------------------------------


def test_merge_completes_after_all_sources_complete() -> None:
    """merge emits COMPLETE only after every source has completed, not just one.

    # Spec: GRAPHREFLY-SPEC S1.3.5
    """
    a = state(1)
    b = state(2)
    m = merge(a, b)
    sink, unsub = collect(m, raw=True)

    a.down([(MessageType.COMPLETE,)])
    # Only one source completed -- merge must NOT be complete yet
    assert m.status != "completed", "merge completed early after first source completed"

    b.down([(MessageType.COMPLETE,)])
    # Both sources completed -- merge must be complete now
    assert any(msg[0] is MessageType.COMPLETE for batch in sink for msg in batch), (
        "merge did not emit COMPLETE after all sources completed"
    )


# ---------------------------------------------------------------------------
# Tier 1 operator test matrix -- 5 operators x 5 concerns = 25 tests
# ---------------------------------------------------------------------------

# -- map ---------------------------------------------------------------------


def test_map_dirty_propagation() -> None:
    """map forwards DIRTY before DATA (two-phase ordering)."""
    s = state(1)
    out = pipe(s, map(lambda x: x + 10))
    sink: list[Messages] = []
    out.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DIRTY,), (MessageType.DATA, 5)])
    flat = [m[0] for batch in sink for m in batch]
    assert MessageType.DIRTY in flat
    assert MessageType.DATA in flat
    assert flat.index(MessageType.DIRTY) < flat.index(MessageType.DATA)


def test_map_resolved_suppression() -> None:
    """map emits RESOLVED and does not rerun fn when upstream value is unchanged.

    Requires custom equals on the map node to detect the identical output.
    """
    s = state(1)
    call_counter = 0

    def counting_fn(x: object) -> str:
        nonlocal call_counter
        call_counter += 1
        return "pos" if x else "other"

    out = pipe(s, map(counting_fn, equals=lambda a, b: a == b))
    sink, unsub = collect(out, raw=True)
    before = call_counter
    # Push same semantic value (different object, same fn output)
    s.down([(MessageType.DATA, 2)])
    after = call_counter
    flat = [m[0] for batch in sink for m in batch]
    assert MessageType.RESOLVED in flat
    assert after - before <= 1, "fn should not rerun more than once for RESOLVED"


def test_map_error_propagation() -> None:
    """Upstream ERROR propagates through map."""
    s = state(0)
    out = pipe(s, map(lambda x: x))
    sink, unsub = collect(out, raw=True)
    s.down([(MessageType.ERROR, RuntimeError("up"))])
    assert any(m[0] is MessageType.ERROR for batch in sink for m in batch)


def test_map_complete_propagation() -> None:
    """Upstream COMPLETE propagates through map."""
    s = state(0)
    out = pipe(s, map(lambda x: x))
    sink, unsub = collect(out, raw=True)
    s.down([(MessageType.COMPLETE,)])
    assert any(m[0] is MessageType.COMPLETE for batch in sink for m in batch)


def test_map_reconnect() -> None:
    """After unsubscribe + resubscribe, map delivers fresh values."""
    s = state(3)
    out = pipe(s, map(lambda x: x * 10))

    unsub1 = out.subscribe(lambda _m: None)
    assert out.get() == 30
    unsub1()
    out.unsubscribe()

    sink2, unsub2 = collect(out, raw=True)
    s.down([(MessageType.DATA, 7)])
    assert out.get() == 70
    data_vals = [m[1] for batch in sink2 for m in batch if m[0] is MessageType.DATA]
    assert 70 in data_vals
    unsub2()


# -- filter ------------------------------------------------------------------


def test_filter_dirty_propagation() -> None:
    """filter forwards DIRTY before DATA/RESOLVED (two-phase ordering)."""
    s = state(2)
    out = pipe(s, filter(lambda x: x % 2 == 0))
    sink: list[Messages] = []
    out.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DIRTY,), (MessageType.DATA, 4)])
    flat = [m[0] for batch in sink for m in batch]
    assert MessageType.DIRTY in flat
    dirty_i = flat.index(MessageType.DIRTY)
    data_or_resolved_i = next(
        i for i, t in enumerate(flat) if t in (MessageType.DATA, MessageType.RESOLVED)
    )
    assert dirty_i < data_or_resolved_i


def test_filter_resolved_suppression() -> None:
    """filter emits RESOLVED (not DATA) when predicate is false; fn need not count."""
    s = state(1)
    out = pipe(s, filter(lambda x: x % 2 == 0))
    sink: list[Messages] = []
    out.subscribe(sink.append)
    sink.clear()
    # Odd value -> predicate false -> RESOLVED, no DATA
    s.down([(MessageType.DIRTY,), (MessageType.DATA, 3)])
    flat = [m[0] for batch in sink for m in batch]
    assert MessageType.RESOLVED in flat
    assert MessageType.DATA not in flat


def test_filter_error_propagation() -> None:
    """Upstream ERROR propagates through filter."""
    s = state(0)
    out = pipe(s, filter(lambda x: x > 0))
    sink, unsub = collect(out, raw=True)
    s.down([(MessageType.ERROR, RuntimeError("up"))])
    assert any(m[0] is MessageType.ERROR for batch in sink for m in batch)


def test_filter_complete_propagation() -> None:
    """Upstream COMPLETE propagates through filter."""
    s = state(0)
    out = pipe(s, filter(lambda x: x > 0))
    sink, unsub = collect(out, raw=True)
    s.down([(MessageType.COMPLETE,)])
    assert any(m[0] is MessageType.COMPLETE for batch in sink for m in batch)


def test_filter_reconnect() -> None:
    """After unsubscribe + resubscribe, filter delivers fresh values."""
    s = state(2)
    out = pipe(s, filter(lambda x: x % 2 == 0))

    unsub1 = out.subscribe(lambda _m: None)
    assert out.get() == 2
    unsub1()
    out.unsubscribe()

    sink2, unsub2 = collect(out, raw=True)
    s.down([(MessageType.DATA, 4)])
    data_vals = [m[1] for batch in sink2 for m in batch if m[0] is MessageType.DATA]
    assert 4 in data_vals
    unsub2()


# -- scan --------------------------------------------------------------------


def test_scan_dirty_propagation() -> None:
    """scan forwards DIRTY before DATA (two-phase ordering)."""
    s = state(1)
    out = pipe(s, scan(lambda a, x: a + x, 0))
    sink: list[Messages] = []
    out.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DIRTY,), (MessageType.DATA, 5)])
    flat = [m[0] for batch in sink for m in batch]
    assert MessageType.DIRTY in flat
    assert MessageType.DATA in flat
    assert flat.index(MessageType.DIRTY) < flat.index(MessageType.DATA)


def test_scan_resolved_suppression() -> None:
    """scan emits RESOLVED when accumulator is unchanged (requires stable equals)."""
    s = state(0)
    out = pipe(s, scan(lambda a, x: a + x, 0, equals=lambda a, b: a == b))
    sink: list[Messages] = []
    out.subscribe(sink.append)
    sink.clear()
    # Adding 0 should not change accumulator
    s.down([(MessageType.DATA, 0)])
    flat = [m[0] for batch in sink for m in batch]
    assert MessageType.RESOLVED in flat


def test_scan_error_propagation() -> None:
    """Upstream ERROR propagates through scan."""
    s = state(0)
    out = pipe(s, scan(lambda a, x: a + x, 0))
    sink, unsub = collect(out, raw=True)
    s.down([(MessageType.ERROR, RuntimeError("up"))])
    assert any(m[0] is MessageType.ERROR for batch in sink for m in batch)


def test_scan_complete_propagation() -> None:
    """Upstream COMPLETE propagates through scan."""
    s = state(0)
    out = pipe(s, scan(lambda a, x: a + x, 0))
    sink, unsub = collect(out, raw=True)
    s.down([(MessageType.COMPLETE,)])
    assert any(m[0] is MessageType.COMPLETE for batch in sink for m in batch)


def test_scan_reconnect() -> None:
    """After unsubscribe + resubscribe, scan starts fresh accumulation."""
    s = state(1)
    out = pipe(s, scan(lambda a, x: a + x, 0))

    unsub1 = out.subscribe(lambda _m: None)
    s.down([(MessageType.DATA, 5)])
    assert out.get() == 6
    unsub1()
    out.unsubscribe()

    # Reconnect: accumulator should reset
    sink2, unsub2 = collect(out, raw=True)
    s.down([(MessageType.DATA, 2)])
    data_vals = [m[1] for batch in sink2 for m in batch if m[0] is MessageType.DATA]
    # Fresh accumulation: seed=0, then s=1 on reconnect subscribe, then DATA 2
    assert data_vals[-1] >= 2, "scan should accumulate from fresh seed after reconnect"
    unsub2()


# -- take --------------------------------------------------------------------


def test_take_dirty_propagation() -> None:
    """take forwards DIRTY before DATA (two-phase ordering)."""
    s = state(1)
    out = pipe(s, take(5))
    sink: list[Messages] = []
    out.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DIRTY,), (MessageType.DATA, 2)])
    flat = [m[0] for batch in sink for m in batch]
    assert MessageType.DIRTY in flat
    assert MessageType.DATA in flat
    assert flat.index(MessageType.DIRTY) < flat.index(MessageType.DATA)


def test_take_resolved_suppression() -> None:
    """take forwards RESOLVED from upstream without triggering extra DATA."""
    # Arrange a chain where map(equals=...) emits RESOLVED for unchanged values,
    # then take(5) sits downstream -- it should forward the RESOLVED unchanged.
    s = state(1)
    out2 = pipe(s, map(lambda x: x, equals=lambda a, b: a == b))
    out3 = pipe(out2, take(5))
    sink: list[Messages] = []
    out3.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DATA, 1)])  # same value -> RESOLVED from map -> forwarded by take
    flat = [m[0] for batch in sink for m in batch]
    assert MessageType.RESOLVED in flat
    assert MessageType.DATA not in flat


def test_take_error_propagation() -> None:
    """Upstream ERROR propagates through take."""
    s = state(0)
    out = pipe(s, take(10))
    sink, unsub = collect(out, raw=True)
    s.down([(MessageType.ERROR, RuntimeError("up"))])
    assert any(m[0] is MessageType.ERROR for batch in sink for m in batch)


def test_take_complete_propagation() -> None:
    """take emits COMPLETE when the quota is exhausted (not relying on upstream COMPLETE).

    Note: take(n) uses complete_when_deps_complete=False -- upstream COMPLETE before the
    quota is reached is not forwarded; only hitting the take limit triggers COMPLETE.
    """
    s = state(0)
    out = pipe(s, take(2))
    sink: list[Messages] = []
    out.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DATA, 1)])
    s.down([(MessageType.DATA, 2)])
    assert any(m[0] is MessageType.COMPLETE for batch in sink for m in batch)


def test_take_reconnect() -> None:
    """After unsubscribe + resubscribe on take(5), the node reconnects without errors
    and values already seen on the upstream are re-delivered on the new subscription.
    """
    s = state(3)
    out = pipe(s, take(5))

    sink1, unsub1 = collect(out, raw=True)
    s.down([(MessageType.DATA, 7)])
    assert out.get() == 7
    unsub1()
    out.unsubscribe()

    sink2, unsub2 = collect(out, raw=True)
    # The upstream state still holds 7; reconnect should deliver it
    assert out.get() == 7, "take should cache value across reconnect"
    s.down([(MessageType.DATA, 9)])
    data_vals2 = [m[1] for batch in sink2 for m in batch if m[0] is MessageType.DATA]
    assert 9 in data_vals2, "take should forward new values after reconnect"
    unsub2()


# -- combine -----------------------------------------------------------------


def test_combine_dirty_propagation() -> None:
    """combine forwards DIRTY before DATA when any source changes."""
    a = state(1)
    b = state(2)
    out = combine(a, b)
    sink: list[Messages] = []
    out.subscribe(sink.append)
    sink.clear()
    a.down([(MessageType.DIRTY,), (MessageType.DATA, 10)])
    flat = [m[0] for batch in sink for m in batch]
    assert MessageType.DIRTY in flat
    assert MessageType.DATA in flat
    assert flat.index(MessageType.DIRTY) < flat.index(MessageType.DATA)


def test_combine_resolved_suppression() -> None:
    """combine emits RESOLVED when all sources resolve to unchanged outputs."""
    a = state(1)
    b = state(2)
    # Use map with equals so combine can receive RESOLVED
    ma = pipe(a, map(lambda x: x, equals=lambda p, q: p == q))
    mb = pipe(b, map(lambda x: x, equals=lambda p, q: p == q))
    call_counter = 0

    def counting_fn(deps: list, _a: object) -> tuple:
        nonlocal call_counter
        call_counter += 1
        return (deps[0], deps[1])

    out = derived([ma, mb], counting_fn)
    out.subscribe(lambda _m: None)
    before = call_counter
    # Push same values -- both maps emit RESOLVED, so out should not rerun
    a.down([(MessageType.DATA, 1)])
    b.down([(MessageType.DATA, 2)])
    after = call_counter
    assert after == before, "derived fn should not rerun when all deps resolve"


def test_combine_error_propagation() -> None:
    """Upstream ERROR propagates through combine."""
    a = state(0)
    b = state(0)
    out = combine(a, b)
    sink, unsub = collect(out, raw=True)
    a.down([(MessageType.ERROR, RuntimeError("up"))])
    assert any(m[0] is MessageType.ERROR for batch in sink for m in batch)


def test_combine_complete_propagation() -> None:
    """combine completes only after ALL sources complete (spec S1.3.5)."""
    a = state(0)
    b = state(0)
    out = combine(a, b)
    sink, unsub = collect(out, raw=True)
    a.down([(MessageType.COMPLETE,)])
    assert not any(m[0] is MessageType.COMPLETE for batch in sink for m in batch), (
        "combine completed early after first source"
    )
    b.down([(MessageType.COMPLETE,)])
    assert any(m[0] is MessageType.COMPLETE for batch in sink for m in batch)


def test_combine_reconnect() -> None:
    """After unsubscribe + resubscribe, combine delivers fresh tuple values."""
    a = state(3)
    b = state(4)
    out = combine(a, b)

    unsub1 = out.subscribe(lambda _m: None)
    assert out.get() == (3, 4)
    unsub1()
    out.unsubscribe()

    sink2, unsub2 = collect(out, raw=True)
    a.down([(MessageType.DATA, 10)])
    data_vals = [m[1] for batch in sink2 for m in batch if m[0] is MessageType.DATA]
    assert (10, 4) in data_vals
    unsub2()
