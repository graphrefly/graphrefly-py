"""Sugar constructors over :func:`graphrefly.core.node.node` (GRAPHREFLY-SPEC §2.7, §4.1)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from graphrefly.core.node import Node, NodeFn, node

type PipeOperator = Callable[[Node[Any]], Node[Any]]


def state(initial: Any, **opts: Any) -> Node[Any]:
    """Manual source: ``node([], null, { initial, ...opts })``."""
    return node([], {**opts, "initial": initial})


def producer(fn: NodeFn, **opts: Any) -> Node[Any]:
    """Auto source: ``node([], fn, opts)`` (same as ``node(fn, **opts)``)."""
    return node(fn, describe_kind="producer", **opts)


def derived(deps: Sequence[Node[Any]], fn: NodeFn, **opts: Any) -> Node[Any]:
    """``node(deps, fn, opts)`` — spec operator pattern is the same primitive."""
    return node(list(deps), fn, describe_kind="derived", **opts)


def effect(deps: Sequence[Node[Any]], fn: NodeFn, **opts: Any) -> Node[Any]:
    """Side-effect leaf: ``node(deps, fn)`` — ``fn`` should return ``None`` (no auto-emit)."""
    return node(list(deps), fn, describe_kind="effect", **opts)


def pipe(source: Node[Any], *ops: PipeOperator) -> Node[Any]:
    """Linear composition; returns the last node. Does not register a Graph."""
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
