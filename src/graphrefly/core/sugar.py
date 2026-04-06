"""Sugar constructors over :func:`graphrefly.core.node.node` (GRAPHREFLY-SPEC §2.7, §4.1)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from graphrefly.core.node import Node, NodeFn, node

type PipeOperator = Callable[[Node[Any]], Node[Any]]


def state(initial: Any, **opts: Any) -> Node[Any]:
    """Create a manually-settable source node with a fixed initial value.

    Args:
        initial: The initial cached value for the node. Because ``initial`` is
            provided, ``equals`` is called on the first ``down()`` emission — if
            the value matches ``initial``, the node emits ``RESOLVED`` instead
            of ``DATA`` (spec §2.5).
        **opts: Additional node options passed through to :func:`~graphrefly.core.node.node`.

    Returns:
        A :class:`~graphrefly.core.node.Node` with no deps and no compute function.

    Example:
        ```python
        from graphrefly import state
        counter = state(0, name="counter")
        counter.down([("DATA", 1)])
        assert counter.get() == 1
        ```
    """
    return node([], {**opts, "initial": initial})


def producer(fn: NodeFn, **opts: Any) -> Node[Any]:
    """Create an auto-starting producer node with no dependencies.

    Args:
        fn: The compute function invoked when the first sink subscribes.
        **opts: Additional node options passed through to :func:`~graphrefly.core.node.node`.

    Returns:
        A :class:`~graphrefly.core.node.Node` whose producer starts on first subscribe.

    Example:
        ```python
        from graphrefly import producer
        from graphrefly.core.protocol import MessageType

        def ticker(deps, actions):
            actions.emit(42)
            actions.down([(MessageType.COMPLETE,)])
            return lambda: None

        p = producer(ticker, name="once")
        ```
    """
    return node(fn, describe_kind="producer", **opts)


def derived(deps: Sequence[Node[Any]], fn: NodeFn, **opts: Any) -> Node[Any]:
    """Create a derived node that recomputes whenever its dependencies settle.

    Args:
        deps: Upstream nodes whose settled values are passed to ``fn``.
        fn: Compute function receiving ``(dep_values, actions)``; may return a
            value to emit or a cleanup callable.
        **opts: Additional node options passed through to :func:`~graphrefly.core.node.node`.

    Returns:
        A :class:`~graphrefly.core.node.Node` that reacts to its upstream deps.

    Example:
        ```python
        from graphrefly import state, derived
        x = state(2)
        doubled = derived([x], lambda deps, _: deps[0] * 2)
        ```
    """
    return node(list(deps), fn, describe_kind="derived", **opts)


def effect(deps: Sequence[Node[Any]], fn: NodeFn, **opts: Any) -> Node[Any]:
    """Create a side-effect leaf node; ``fn`` should return ``None`` (no auto-emit).

    Args:
        deps: Upstream nodes whose settled values trigger ``fn``.
        fn: Side-effect function receiving ``(dep_values, actions)``; return value
            is ignored unless it is a cleanup callable.
        **opts: Additional node options passed through to :func:`~graphrefly.core.node.node`.

    Returns:
        A :class:`~graphrefly.core.node.Node` that runs ``fn`` on each settlement
        but does not emit reactive values downstream.

    Example:
        ```python
        from graphrefly import state, effect
        x = state(0)
        log = []
        e = effect([x], lambda deps, _: log.append(deps[0]))
        x.down([("DATA", 1)])
        ```
    """
    return node(list(deps), fn, describe_kind="effect", **opts)


def pipe(source: Node[Any], *ops: PipeOperator) -> Node[Any]:
    """Compose a linear pipeline of unary operators over ``source``.

    Args:
        source: The root node to pipe through operators.
        *ops: Unary operator callables each transforming ``Node -> Node``.

    Returns:
        The last node in the pipeline (result of applying all operators in order).

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra import map_val, filter_val
        x = state(0)
        result = pipe(x, map_val(lambda v: v * 2), filter_val(lambda v: v > 0))
        ```
    """
    cur: Node[Any] = source
    for op in ops:
        cur = op(cur)
    return cur


__all__ = [
    "PipeOperator",
    "derived",
    "effect",
    "pipe",
    "producer",
    "state",
]
