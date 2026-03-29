"""Tier 1 extra operators (roadmap 2.1) — built on ``node`` / ``derived``."""

from __future__ import annotations

from graphrefly.core import Messages, MessageType, derived, state
from graphrefly.core.sugar import pipe
from graphrefly.extra.tier1 import (
    combine,
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
    start_with,
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
    s = state(1)
    t = pipe(s, take(2))
    sink: list[Messages] = []
    t.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DATA, 2)])
    s.down([(MessageType.DATA, 3)])
    assert t.get() == 3
    assert any(m[0] is MessageType.COMPLETE for m in _msgs(sink))


def test_skip_drops_first_n() -> None:
    s = state(0)
    sk = pipe(s, skip(2))
    sink: list[Messages] = []
    sk.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DATA, 1)])
    s.down([(MessageType.DATA, 2)])
    s.down([(MessageType.DATA, 3)])
    data_vals = [m[1] for m in _msgs(sink) if m[0] is MessageType.DATA]
    assert data_vals == [3]


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


def test_start_with_then_src() -> None:
    s = state(5)
    out = pipe(s, start_with(0))
    sink: list[Messages] = []
    out.subscribe(sink.append)
    data = [m[1] for m in _msgs(sink) if m[0] is MessageType.DATA]
    assert data[0] == 0
    assert data[1] == 5


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
    s = state(0)
    e = pipe(s, element_at(2))
    sink: list[Messages] = []
    e.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DATA, 1)])
    s.down([(MessageType.DATA, 2)])
    s.down([(MessageType.DATA, 99)])
    data_vals = [m[1] for m in _msgs(sink) if m[0] is MessageType.DATA]
    assert data_vals == [99]


def test_first_is_take_one() -> None:
    s = state(1)
    f = pipe(s, first())
    sink: list[Messages] = []
    f.subscribe(sink.append)
    sink.clear()
    s.down([(MessageType.DATA, 2)])
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
    stop = state(False)
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
    a = state(1)
    b = state(2)
    r = race(a, b)
    sink: list[Messages] = []
    r.subscribe(sink.append)
    sink.clear()
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
