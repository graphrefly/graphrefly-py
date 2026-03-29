"""Roadmap §2.3 — sources, sinks, multicast (``node`` / ``producer`` under the hood)."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from graphrefly.core import MessageType
from graphrefly.core.node import NodeImpl
from graphrefly.core.sugar import state
from graphrefly.extra.sources import (
    cached,
    empty,
    first_value_from,
    first_value_from_future,
    for_each,
    from_any,
    from_async_iter,
    from_awaitable,
    from_cron,
    from_event_emitter,
    from_iter,
    from_timer,
    never,
    of,
    replay,
    share,
    throw_error,
    to_array,
    to_list,
)


def test_of_to_list() -> None:
    assert to_list(of(1, 2, 3)) == [1, 2, 3]


def test_empty_to_list() -> None:
    assert to_list(empty()) == []


def test_never_times_out() -> None:
    with pytest.raises(TimeoutError, match="timed out"):
        to_list(never(), timeout=0.05)


def test_throw_error_to_list() -> None:
    err = ValueError("x")
    with pytest.raises(ValueError, match="x"):
        to_list(throw_error(err))


def test_from_iter() -> None:
    assert to_list(from_iter([10, 20])) == [10, 20]


def test_from_iter_error() -> None:
    def bad() -> object:
        raise RuntimeError("boom")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError, match="boom"):
        to_list(from_iter(bad()))


def test_from_timer_oneshot() -> None:
    assert to_list(from_timer(0.02, None, first=7)) == [7]


def test_from_timer_periodic() -> None:
    n = from_timer(0.01, 0.05, first=0)
    out: list[Any] = []
    done = threading.Event()

    def sink(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                out.append(m[1])
            if len(out) >= 3:
                done.set()

    unsub = n.subscribe(sink)
    assert done.wait(timeout=2.0)
    unsub()
    assert out == [0, 1, 2]


def test_from_cron_subscribe_unsubscribe() -> None:
    n = from_cron("* * * * *")
    unsub = n.subscribe(lambda _m: None)
    time.sleep(0.02)
    unsub()


def test_from_awaitable() -> None:
    async def slow() -> int:
        await asyncio.sleep(0.02)
        return 99

    assert to_list(from_awaitable(slow())) == [99]


def test_from_async_iter() -> None:
    async def gen() -> object:
        yield 1
        yield 2

    assert to_list(from_async_iter(gen())) == [1, 2]


def test_from_any_node_passthrough() -> None:
    s = of(1)
    assert from_any(s) is s


def test_from_any_list() -> None:
    assert to_list(from_any([5, 6])) == [5, 6]


def test_from_any_scalar() -> None:
    assert to_list(from_any(42)) == [42]


def test_first_value_from_and_empty() -> None:
    assert first_value_from(of(8)) == 8
    with pytest.raises(StopIteration):
        first_value_from(empty())


def test_first_value_from_future() -> None:
    fut = first_value_from_future(of(3))
    assert fut.result(timeout=2.0) == 3
    fut2 = first_value_from_future(empty())
    with pytest.raises(LookupError, match="without DATA"):
        fut2.result(timeout=2.0)


def test_for_each() -> None:
    acc: list[Any] = []
    u = for_each(of(1, 2), acc.append)
    time.sleep(0.05)
    u()
    assert acc == [1, 2]


def test_for_each_error_with_handler() -> None:
    """Prefer ``on_error`` for ERROR tuples (default raise may not surface to caller)."""
    seen: list[BaseException] = []

    def on_err(e: BaseException) -> None:
        seen.append(e)

    u = for_each(throw_error(ValueError("bad")), lambda _x: None, on_error=on_err)
    time.sleep(0.05)
    u()
    assert len(seen) == 1
    assert str(seen[0]) == "bad"


def test_share_multicast_wires_one_upstream() -> None:
    """``share`` is a no-fn dep wire: one subscription to the source, fan-out to many sinks."""
    root = state(0)
    src = share(root)
    a: list[Any] = []
    b: list[Any] = []

    def sink_a(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                a.append(m[1])

    def sink_b(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                b.append(m[1])

    ua = src.subscribe(sink_a)
    ub = src.subscribe(sink_b)
    root.down([(MessageType.DIRTY,), (MessageType.DATA, 7)])
    deadline = time.time() + 2.0
    while time.time() < deadline and (len(a) < 1 or len(b) < 1):
        time.sleep(0.01)
    ua()
    ub()
    assert a == [7]
    assert b == [7]


def test_cached_and_replay_are_nodes() -> None:
    s = of(1)
    assert isinstance(cached(s), NodeImpl)
    assert isinstance(replay(s, buffer_size=3), NodeImpl)


def test_replay_buffer_size_negative() -> None:
    with pytest.raises(ValueError, match="buffer_size"):
        replay(of(1), buffer_size=-1)


def test_replay_buffer_replays_to_late_subscriber() -> None:
    """Push values through replay, subscribe late, verify buffer replayed."""
    root = state(0)
    r = replay(root, buffer_size=3)

    # First subscriber to activate the chain
    first_vals: list[Any] = []

    def sink_first(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                first_vals.append(m[1])

    u1 = r.subscribe(sink_first)

    # Push values through root
    for v in [10, 20, 30, 40]:
        root.down([(MessageType.DIRTY,), (MessageType.DATA, v)])
        time.sleep(0.02)

    # Late subscriber should get last 3 buffered values
    late_vals: list[Any] = []

    def sink_late(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                late_vals.append(m[1])

    u2 = r.subscribe(sink_late)
    time.sleep(0.05)

    u1()
    u2()
    # Late subscriber should have received replayed [20, 30, 40]
    assert late_vals[:3] == [20, 30, 40]


def test_replay_buffer_size_one() -> None:
    """cached replays last value via replay(buffer_size=1) semantics."""
    root = state(0)
    r = replay(root, buffer_size=1)

    first_vals: list[Any] = []

    def sink_first(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                first_vals.append(m[1])

    u1 = r.subscribe(sink_first)

    root.down([(MessageType.DIRTY,), (MessageType.DATA, 99)])
    time.sleep(0.02)

    late_vals: list[Any] = []

    def sink_late(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                late_vals.append(m[1])

    u2 = r.subscribe(sink_late)
    time.sleep(0.05)

    u1()
    u2()
    assert late_vals[0] == 99


def test_replay_rejects_zero_buffer() -> None:
    with pytest.raises(ValueError, match="buffer_size must be >= 1"):
        replay(of(1), buffer_size=0)


def test_share_initial_value() -> None:
    s = state(42)
    shared = share(s)
    assert shared.get() == 42


def test_from_cron_builtin_parser() -> None:
    """Verify from_cron works without croniter (built-in parser)."""
    n = from_cron("* * * * *")
    out: list[Any] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                out.append(m[1])

    unsub = n.subscribe(sink)
    # The cron fires immediately if current minute matches "* * * * *"
    time.sleep(0.1)
    unsub()
    # "* * * * *" matches every minute, so it should fire once on first check
    assert len(out) >= 1
    assert isinstance(out[0], int)  # time_ns returns int


def test_from_event_emitter() -> None:
    """Simple emitter class, verify DATA received."""

    class SimpleEmitter:
        def __init__(self) -> None:
            self._listeners: dict[str, list] = {}

        def add_listener(self, event: str, fn: Any) -> None:
            self._listeners.setdefault(event, []).append(fn)

        def remove_listener(self, event: str, fn: Any) -> None:
            if event in self._listeners:
                self._listeners[event] = [f for f in self._listeners[event] if f is not fn]

        def emit(self, event: str, *args: Any) -> None:
            for fn in self._listeners.get(event, []):
                fn(*args)

    emitter = SimpleEmitter()
    n = from_event_emitter(emitter, "data")
    out: list[Any] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                out.append(m[1])

    unsub = n.subscribe(sink)
    emitter.emit("data", "hello")
    emitter.emit("data", "world")
    time.sleep(0.05)
    unsub()
    assert out == ["hello", "world"]


def test_to_array_reactive() -> None:
    """of(1,2,3) | to_array -> Node that emits [1,2,3]."""
    result = to_list(of(1, 2, 3) | to_array)
    assert result == [[1, 2, 3]]
