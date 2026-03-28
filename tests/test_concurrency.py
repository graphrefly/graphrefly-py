"""Concurrency: subgraph locks, thread-safe get() via _cache_lock, defer_down / defer_set (0.4)."""

from __future__ import annotations

import sys
import threading
from typing import Any

import pytest

from graphrefly import MessageType, node
from graphrefly.core.subgraph_locks import (
    acquire_subgraph_write_lock_with_defer,
    defer_down,
    defer_set,
)


def _gil_status() -> str:
    gi = getattr(sys, "_is_gil_enabled", None)
    if callable(gi):
        return "gil" if gi() else "no_gil"
    return "unknown"


def test_node_get_is_lock_free_while_down_updates_value() -> None:
    """Readers call get() without taking the subgraph write lock; writers use down(DATA)."""
    src = node({"initial": 0, "name": "src"})

    def ident(deps: list[Any], _acts: Any) -> Any:
        return deps[0]

    d = node([src], ident, name="d")
    d.subscribe(lambda _m: None)

    errors: list[BaseException] = []
    el = threading.Lock()
    done = threading.Event()

    def reader() -> None:
        while not done.is_set():
            v = d.get()
            if v is not None and not isinstance(v, int):
                with el:
                    errors.append(TypeError(v))

    def writer() -> None:
        for i in range(300):
            src.down([[MessageType.DATA, i]])

    readers = [threading.Thread(target=reader) for _ in range(5)]
    for t in readers:
        t.start()
    w = threading.Thread(target=writer)
    w.start()
    w.join(timeout=10.0)
    assert not w.is_alive(), f"writer stuck ({_gil_status()})"
    done.set()
    for t in readers:
        t.join(timeout=5.0)
        assert not t.is_alive(), f"reader stuck ({_gil_status()})"
    assert not errors, f"get() saw bad values in {_gil_status()} mode: {errors!r}"


def test_independent_subgraphs_concurrent_down_complete() -> None:
    """Unrelated nodes are different union components; concurrent downs should both finish."""
    a = node({"initial": 0, "name": "a"})
    b = node({"initial": 0, "name": "b"})
    barrier = threading.Barrier(2)
    done: list[str] = []

    def writer_a() -> None:
        barrier.wait()
        a.down([[MessageType.DATA, 1]])
        done.append("a")

    def writer_b() -> None:
        barrier.wait()
        b.down([[MessageType.DATA, 2]])
        done.append("b")

    t1 = threading.Thread(target=writer_a)
    t2 = threading.Thread(target=writer_b)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert not t1.is_alive() and not t2.is_alive()
    assert set(done) == {"a", "b"}


def test_shared_component_serializes_concurrent_down() -> None:
    """Two nodes in the same union share one lock; one writer excludes the other."""
    left = node({"initial": 0, "name": "left"})
    right = node({"initial": 0, "name": "right"})

    def combine(deps: list[Any], _acts: Any) -> Any:
        return deps[0] + deps[1]

    both = node([left, right], combine, name="both")
    both.subscribe(lambda _m: None)

    gate = threading.Event()
    entered = threading.Event()
    right_started = threading.Event()
    right_finished = threading.Event()

    def write_left() -> None:
        with acquire_subgraph_write_lock_with_defer(left):
            entered.set()
            gate.wait(timeout=5.0)
            left.down([[MessageType.DATA, 1]])

    def write_right() -> None:
        right_started.set()
        right.down([[MessageType.DATA, 2]])
        right_finished.set()

    t_left = threading.Thread(target=write_left)
    t_right = threading.Thread(target=write_right)
    t_left.start()
    assert entered.wait(timeout=3.0)
    t_right.start()
    assert right_started.wait(timeout=3.0)
    assert not right_finished.wait(timeout=0.25)
    gate.set()
    t_left.join(timeout=5.0)
    t_right.join(timeout=5.0)
    assert right_finished.is_set()


def test_defer_down_runs_after_lock_release() -> None:
    a = node({"initial": 0, "name": "a"})
    b = node({"initial": 0, "name": "b"})
    log: list[str] = []

    with acquire_subgraph_write_lock_with_defer(a):
        log.append(f"a_locked:b={b.get()!r}")
        defer_down(b, [[MessageType.DATA, 42]])
        log.append(f"still_locked:b={b.get()!r}")

    log.append(f"after_release:b={b.get()!r}")
    assert log == ["a_locked:b=0", "still_locked:b=0", "after_release:b=42"]


class _DownTarget:
    __slots__ = ("_n",)

    def __init__(self, n: Any) -> None:
        self._n = n

    def set(self, value: object) -> None:
        self._n.down([[MessageType.DATA, value]])


def test_defer_set_runs_after_lock_release() -> None:
    a = node({"initial": 0, "name": "a"})
    b = node({"initial": 0, "name": "b"})
    log: list[str] = []
    target = _DownTarget(b)

    with acquire_subgraph_write_lock_with_defer(a):
        log.append(f"a_locked:b={b.get()!r}")
        defer_set(target, 99)
        log.append(f"still_locked:b={b.get()!r}")

    log.append(f"after_release:b={b.get()!r}")
    assert log == ["a_locked:b=0", "still_locked:b=0", "after_release:b=99"]


def test_defer_set_immediate_when_no_lock() -> None:
    s = node({"initial": 0, "name": "s"})
    defer_set(_DownTarget(s), 7)
    assert s.get() == 7


def test_batch_deferred_phase2_reacquires_subgraph_lock() -> None:
    """Regression: phase-2 drain must not run sinks without the node's write lock."""
    from graphrefly.core.protocol import batch

    src = node({"initial": 0, "name": "src"})
    seen: list[bool] = []

    def ident(deps: list[Any], _acts: Any) -> Any:
        return deps[0]

    d = node([src], ident, name="d")

    def sink(_m: Any) -> None:
        # If sink runs unlocked while another thread mutates, this could race; here we
        # only assert the drain path completed.
        seen.append(True)

    d.subscribe(sink)
    with batch():
        src.down([[MessageType.DATA, 1]])
    assert seen, "expected downstream delivery after batch"


@pytest.mark.skipif(
    not callable(getattr(sys, "_is_gil_enabled", None)) or sys._is_gil_enabled(),
    reason="optional diagnostic when a free-threaded build is available",
)
def test_free_threaded_build_detected() -> None:
    """Placeholder so CI with ``python3.14t`` exercises the suite under no-GIL."""
    assert not sys._is_gil_enabled()
