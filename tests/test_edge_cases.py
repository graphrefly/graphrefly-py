"""Edge-case tests for operators — P0 (data-loss) and P1 (correctness).

Cross-referenced from docs/batch-review/batch-7.md.
"""

from __future__ import annotations

import time
from typing import Any

from graphrefly.core import Messages, MessageType, state
from graphrefly.core.protocol import batch
from graphrefly.core.sugar import derived, pipe
from graphrefly.extra.tier1 import combine, concat, map, merge
from graphrefly.extra.tier2 import (
    concat_map,
    debounce,
    exhaust_map,
    switch_map,
    throttle,
    timeout,
)


def _values(sink: list[Messages]) -> list[Any]:
    out: list[Any] = []
    for b in sink:
        for m in b:
            if m[0] is MessageType.DATA:
                out.append(m[1])
    return out


def _has(sink: list[Messages], mt: MessageType) -> bool:
    return any(m[0] is mt for b in sink for m in b)


def _count(sink: list[Messages], mt: MessageType) -> int:
    return sum(1 for b in sink for m in b if m[0] is mt)


# --- P0: Data-loss edge cases ---


def test_debounce_flush_on_complete() -> None:
    """Debounce flushes pending value when source completes."""
    s = state(0)
    d = pipe(s, debounce(0.05))
    sink: list[Messages] = []
    d.subscribe(sink.append)

    s.down([(MessageType.DIRTY,)])
    s.down([(MessageType.DATA, 42)])

    # Source completes before timer fires
    s.down([(MessageType.COMPLETE,)])

    time.sleep(0.1)

    assert 42 in _values(sink)
    assert _has(sink, MessageType.COMPLETE)


def test_debounce_cancel_on_error() -> None:
    """Debounce cancels pending on upstream ERROR."""
    s = state(0)
    d = pipe(s, debounce(0.05))
    sink: list[Messages] = []
    d.subscribe(sink.append)

    s.down([(MessageType.DIRTY,)])
    s.down([(MessageType.DATA, 42)])
    s.down([(MessageType.ERROR, RuntimeError("fail"))])

    time.sleep(0.1)
    assert _has(sink, MessageType.ERROR)


def test_debounce_complete_without_pending() -> None:
    """Debounce forwards COMPLETE even without pending."""
    s = state(0)
    d = pipe(s, debounce(0.05))
    sink: list[Messages] = []
    d.subscribe(sink.append)

    s.down([(MessageType.COMPLETE,)])
    assert _has(sink, MessageType.COMPLETE)


def test_throttle_forwards_complete() -> None:
    """Throttle forwards upstream COMPLETE."""
    s = state(0)
    t = pipe(s, throttle(0.1))
    sink: list[Messages] = []
    t.subscribe(sink.append)

    s.down([(MessageType.COMPLETE,)])
    assert _has(sink, MessageType.COMPLETE)


def test_throttle_forwards_error() -> None:
    """Throttle forwards upstream ERROR."""
    s = state(0)
    t = pipe(s, throttle(0.1))
    sink: list[Messages] = []
    t.subscribe(sink.append)

    s.down([(MessageType.ERROR, RuntimeError("x"))])
    assert _has(sink, MessageType.ERROR)


def test_timeout_clears_on_complete() -> None:
    """Timeout timer is cleared when source completes."""
    s = state(0)
    t = pipe(s, timeout(0.05))
    sink: list[Messages] = []
    t.subscribe(sink.append)

    s.down([(MessageType.COMPLETE,)])
    time.sleep(0.1)

    assert not _has(sink, MessageType.ERROR)
    assert _has(sink, MessageType.COMPLETE)


def test_timeout_clears_on_error() -> None:
    """Timeout timer is cleared when source errors."""
    s = state(0)
    t = pipe(s, timeout(0.05))
    sink: list[Messages] = []
    t.subscribe(sink.append)

    s.down([(MessageType.ERROR, RuntimeError("upstream"))])
    time.sleep(0.1)

    assert _count(sink, MessageType.ERROR) == 1


def test_merge_all_complete() -> None:
    """Merge completes only after ALL sources complete."""
    s1 = state(1)
    s2 = state(2)
    m = merge(s1, s2)
    sink: list[Messages] = []
    m.subscribe(sink.append)

    s1.down([(MessageType.COMPLETE,)])
    assert not _has(sink, MessageType.COMPLETE)

    s2.down([(MessageType.COMPLETE,)])
    assert _has(sink, MessageType.COMPLETE)


def test_merge_error_propagates() -> None:
    """Error from one merge source propagates immediately."""
    s1 = state(1)
    s2 = state(2)
    m = merge(s1, s2)
    sink: list[Messages] = []
    m.subscribe(sink.append)

    s1.down([(MessageType.ERROR, RuntimeError("fail"))])
    assert _has(sink, MessageType.ERROR)


def test_merge_independent_values() -> None:
    """Merge forwards values from each source independently."""
    s1 = state(0)
    s2 = state(0)
    m = merge(s1, s2)
    sink: list[Messages] = []
    m.subscribe(sink.append)

    with batch():
        s1.down([(MessageType.DIRTY,)])
        s1.down([(MessageType.DATA, 10)])
    with batch():
        s2.down([(MessageType.DIRTY,)])
        s2.down([(MessageType.DATA, 20)])

    vals = _values(sink)
    assert 10 in vals
    assert 20 in vals


# --- P1: Correctness edge cases ---


def test_switchmap_waits_for_inner_on_outer_complete() -> None:
    """switchMap waits for active inner to complete before emitting COMPLETE."""
    outer = state(0)
    inner = state(10)
    out = pipe(outer, switch_map(lambda _v: inner))
    sink: list[Messages] = []
    out.subscribe(sink.append)

    outer.down([(MessageType.COMPLETE,)])
    assert not _has(sink, MessageType.COMPLETE)

    inner.down([(MessageType.COMPLETE,)])
    assert _has(sink, MessageType.COMPLETE)


def test_switchmap_inner_error_propagates() -> None:
    """Inner error in switchMap propagates to output."""
    outer = state(0)
    inner = state(10)
    out = pipe(outer, switch_map(lambda _v: inner))
    sink: list[Messages] = []
    out.subscribe(sink.append)

    inner.down([(MessageType.ERROR, RuntimeError("inner-fail"))])
    assert _has(sink, MessageType.ERROR)


def test_concatmap_inner_error_propagates() -> None:
    """Inner error in concatMap propagates to output."""
    outer = state(0)
    inner = state(10)
    out = pipe(outer, concat_map(lambda _v: inner))
    sink: list[Messages] = []
    out.subscribe(sink.append)

    inner.down([(MessageType.ERROR, RuntimeError("inner-err"))])
    assert _has(sink, MessageType.ERROR)


def test_exhaustmap_inner_error_propagates() -> None:
    """Inner error in exhaustMap propagates to output."""
    outer = state(0)
    inner = state(10)
    out = pipe(outer, exhaust_map(lambda _v: inner))
    sink: list[Messages] = []
    out.subscribe(sink.append)

    inner.down([(MessageType.ERROR, RuntimeError("inner-err"))])
    assert _has(sink, MessageType.ERROR)


def test_diamond_glitch_freedom() -> None:
    """combine never exposes intermediate state in a diamond topology."""
    root = state(1)
    left = pipe(root, map(lambda x: x * 2))
    right = pipe(root, map(lambda x: x + 10))
    joined = combine(left, right)

    snapshots: list[Any] = []
    joined.subscribe(lambda msgs: snapshots.extend(m[1] for m in msgs if m[0] is MessageType.DATA))

    assert len(snapshots) >= 1
    initial = snapshots[-1]
    assert list(initial) == [2, 11]

    snapshots.clear()
    with batch():
        root.down([(MessageType.DIRTY,)])
        root.down([(MessageType.DATA, 5)])

    for snap in snapshots:
        l_val, r_val = snap[0], snap[1]
        assert l_val == (r_val - 10) * 2, f"Glitch detected: {snap}"

    if snapshots:
        assert list(snapshots[-1]) == [10, 15]


def test_reentrancy_subscriber_state_change() -> None:
    """Subscriber modifying state during emission produces consistent final value."""
    s = state(0)
    d = derived([s], lambda deps, _a: deps[0])
    values: list[int] = []

    def sink(msgs: Messages) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                v = m[1]
                values.append(v)
                if v == 1:
                    with batch():
                        s.down([(MessageType.DIRTY,)])
                        s.down([(MessageType.DATA, 2)])

    d.subscribe(sink)

    with batch():
        s.down([(MessageType.DIRTY,)])
        s.down([(MessageType.DATA, 1)])

    assert d.get() == 2
    assert 2 in values


def test_combine_error_propagates() -> None:
    """Error from any combine source propagates."""
    s1 = state(1)
    s2 = state(2)
    c = combine(s1, s2)
    sink: list[Messages] = []
    c.subscribe(sink.append)

    s1.down([(MessageType.ERROR, RuntimeError("boom"))])
    assert _has(sink, MessageType.ERROR)


def test_concat_error_stops_chain() -> None:
    """Error from first concat source stops chain."""
    s1 = state(1)
    s2 = state(2)
    c = pipe(s1, concat(s2))
    sink: list[Messages] = []
    c.subscribe(sink.append)

    s1.down([(MessageType.ERROR, RuntimeError("first-err"))])
    assert _has(sink, MessageType.ERROR)


def test_batch_coalesces_to_final_value() -> None:
    """Batch coalesces to final value."""
    s = state(0)
    d = derived([s], lambda deps, _a: deps[0])
    sink: list[Messages] = []
    d.subscribe(sink.append)

    with batch():
        s.down([(MessageType.DIRTY,)])
        s.down([(MessageType.DATA, 1)])
        s.down([(MessageType.DIRTY,)])
        s.down([(MessageType.DATA, 2)])
        s.down([(MessageType.DIRTY,)])
        s.down([(MessageType.DATA, 3)])

    vals = _values(sink)
    assert vals[-1] == 3
