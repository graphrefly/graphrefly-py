"""Sugar constructors — aligned with graphrefly-ts (no ``subscribe`` / ``operator`` sugar)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from graphrefly.core import node
from graphrefly.core.meta import describe_node
from graphrefly.core.protocol import Messages, MessageType
from graphrefly.core.sugar import (
    derived,
    effect,
    pipe,
    producer,
    state,
)

if TYPE_CHECKING:
    from graphrefly.core.node import Node


def _doubled(n: Node) -> Node:
    return derived([n], lambda d, _a: d[0] * 2)


def test_state_is_manual_source_with_initial() -> None:
    s = state(10)
    assert s.get() == 10
    seen: list[object] = []

    def sink(msgs: Messages) -> None:
        for m in msgs:
            seen.append(m[0])

    unsub = s.subscribe(sink)
    s.down([(MessageType.DIRTY,), (MessageType.DATA, 11)])
    unsub()
    assert s.get() == 11
    assert MessageType.DIRTY in seen
    assert MessageType.DATA in seen


def test_producer_runs_on_subscribe_and_can_emit() -> None:
    p = producer(lambda _deps, a: a.emit(1))
    seen: list[int] = []

    def sink(msgs: Messages) -> None:
        for m in msgs:
            if m[0] == MessageType.DATA:
                seen.append(m[1])

    unsub = p.subscribe(sink)
    assert p.get() == 1
    # Producer emits 1 during connect; START handshake delivers cached value.
    assert seen == [1]
    unsub()


def test_derived_deps_and_value_returning_fn() -> None:
    src = state(2)
    d = derived([src], lambda dps, _a: dps[0] * 3)
    seen: list[object] = []

    def sink(msgs: Messages) -> None:
        for m in msgs:
            seen.append(m[0])

    unsub = d.subscribe(sink)
    src.down([(MessageType.DATA, 3)])
    assert d.get() == 9
    assert MessageType.DATA in seen
    unsub()


def test_effect_and_producer_set_describe_kind() -> None:
    e = effect([state(0)], lambda _d, _a: None)
    assert describe_node(e)["type"] == "effect"
    p = producer(lambda _d, a: a.emit(1))
    assert describe_node(p)["type"] == "producer"
    d = derived([state(0)], lambda deps, _a: deps[0])
    assert describe_node(d)["type"] == "derived"


def test_effect_runs_without_auto_emit_from_return() -> None:
    src = state(0)
    runs = 0

    def fn(_d: list[Any], _a: Any) -> None:
        nonlocal runs
        runs += 1

    e = effect([src], fn)
    unsub = e.subscribe(lambda _m: None)
    src.down([(MessageType.DIRTY,), (MessageType.DATA, 1)])
    unsub()
    assert runs == 2
    assert e.get() is None


def test_pipe_chains_unary_transforms() -> None:
    src = state(1)
    out = pipe(src, _doubled, _doubled)
    unsub = out.subscribe(lambda _m: None)
    assert out.get() == 4
    unsub()


def test_pipe_with_no_ops_returns_source() -> None:
    src = state("x")
    assert pipe(src) is src


def test_derived_with_explicit_down_skips_return_value_emit() -> None:
    src = state(0)

    def fn(_d: list[Any], a: Any) -> int:
        a.down([(MessageType.DIRTY,), (MessageType.RESOLVED,)])
        return 999

    d = derived([src], fn)
    vals: list[object] = []

    def sink(msgs: Messages) -> None:
        for m in msgs:
            if m[0] == MessageType.DATA:
                vals.append(m[1])

    unsub = d.subscribe(sink)
    src.down([(MessageType.DIRTY,), (MessageType.DATA, 1)])
    unsub()
    assert vals == []


def test_or_operator_pipes_unary_op() -> None:
    src = state(1)
    out = src | _doubled | _doubled
    unsub = out.subscribe(lambda _m: None)
    assert out.get() == 4
    unsub()


def test_single_dep_wire_uses_node_like_ts() -> None:
    """Roadmap: no ``subscribe(dep, fn)`` sugar — use ``node([dep], fn)``."""
    src = state(5)
    w = node([src], lambda d, _a: d[0] + 1)
    unsub = w.subscribe(lambda _m: None)
    assert w.get() == 6
    unsub()


def test_single_dep_wire_teardown_with_subscribe_and_unsubscribe() -> None:
    """Lifecycle without ``subscribing()`` CM: sink + ``node.unsubscribe()``."""
    src = state(0)
    runs = 0

    def fn(_d: list[Any], _a: Any) -> None:
        nonlocal runs
        runs += 1

    w = node([src], fn)
    unsub_sink = w.subscribe(lambda _m: None)
    try:
        assert runs == 1
        src.down([(MessageType.DATA, 1)])
        assert runs == 2
        assert w.get() is None
    finally:
        unsub_sink()
        w.unsubscribe()

    src.down([(MessageType.DATA, 2)])
    assert runs == 2
