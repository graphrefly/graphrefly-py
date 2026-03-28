"""Graph container — GRAPHREFLY-SPEC §3.1–3.3 (Phase 1.1)."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from graphrefly.core.protocol import MessageType

if TYPE_CHECKING:
    from collections.abc import Iterator

    from graphrefly.core.node import NodeImpl


class Graph:
    """Named registry of nodes with explicit edges (pure wires, no transforms).

    When ``thread_safe`` is true (default), registry mutations serialize on an
    :class:`threading.RLock`. :meth:`remove` pops under that lock, then sends
    ``[[TEARDOWN]]`` outside the lock to avoid deadlocking against per-node
    subgraph write locks.

    :meth:`connect` is idempotent if the edge is already registered.
    :meth:`disconnect` removes a registered edge and raises :exc:`ValueError`
    if that edge was never recorded. It does not mutate ``NodeImpl`` dependency
    lists (see ``docs/optimizations.md``).
    """

    __slots__ = ("_edges", "_lock", "_name", "_nodes", "_thread_safe")

    def __init__(self, name: str, opts: dict[str, Any] | None = None) -> None:
        """Create a graph. ``opts`` may include ``thread_safe`` (default ``True``)."""
        if not name:
            msg = "Graph name must be non-empty"
            raise ValueError(msg)
        o = dict(opts or {})
        self._name = name
        self._thread_safe = bool(o.get("thread_safe", True))
        self._lock: threading.RLock | None = threading.RLock() if self._thread_safe else None
        self._nodes: dict[str, NodeImpl[Any]] = {}
        self._edges: set[tuple[str, str]] = set()

    @property
    def name(self) -> str:
        return self._name

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock = self._lock
        if lock is not None:
            with lock:
                yield
        else:
            yield

    def add(self, node_name: str, n: NodeImpl[Any]) -> None:
        """Register ``n`` under ``node_name``; sets ``n``'s graph name if unset."""
        if not node_name:
            raise ValueError("node name must be non-empty")
        with self._locked():
            if node_name in self._nodes:
                raise KeyError(f"duplicate node name: {node_name!r}")
            for existing_name, existing in self._nodes.items():
                if existing is n:
                    raise ValueError(f"node instance already registered as {existing_name!r}")
            if n._name is None:
                object.__setattr__(n, "_name", node_name)
            self._nodes[node_name] = n

    def remove(self, node_name: str) -> None:
        """Unregister a node and send ``[[TEARDOWN]]`` (spec: unregister and teardown)."""
        with self._locked():
            if node_name not in self._nodes:
                raise KeyError(node_name)
            n = self._nodes.pop(node_name)
            self._edges = {(f, t) for f, t in self._edges if f != node_name and t != node_name}
        n.down([(MessageType.TEARDOWN,)])

    def node(self, node_name: str) -> NodeImpl[Any]:
        """Return the registered node object."""
        with self._locked():
            try:
                return self._nodes[node_name]
            except KeyError as e:
                raise KeyError(node_name) from e

    def get(self, node_name: str) -> Any:
        """Shorthand for ``graph.node(name).get()``."""
        return self.node(node_name).get()

    def set(self, node_name: str, value: Any) -> None:
        """Shorthand for ``graph.node(name).down([[DATA, value]])``."""
        self.node(node_name).down([(MessageType.DATA, value)])

    def connect(self, from_name: str, to_name: str) -> None:
        """Record a pure wire; ``to`` must already list ``from`` as a constructor dependency."""
        if not from_name or not to_name:
            msg = "connect/disconnect names must be non-empty"
            raise ValueError(msg)
        with self._locked():
            key = (from_name, to_name)
            if key in self._edges:
                return
            try:
                from_n = self._nodes[from_name]
            except KeyError as e:
                raise KeyError(f"unknown node: {from_name!r}") from e
            try:
                to_n = self._nodes[to_name]
            except KeyError as e:
                raise KeyError(f"unknown node: {to_name!r}") from e
            if from_n is to_n:
                raise ValueError("cannot connect a node to itself")
            if not any(d is from_n for d in to_n._deps):
                raise ValueError(
                    f"connect({from_name!r}, {to_name!r}): {to_name!r} must include "
                    f"{from_name!r} in its dependency list at construction (pure wire)"
                )
            self._edges.add(key)

    def disconnect(self, from_name: str, to_name: str) -> None:
        """Remove a registered edge (bookkeeping only; see class docstring)."""
        if not from_name or not to_name:
            msg = "connect/disconnect names must be non-empty"
            raise ValueError(msg)
        with self._locked():
            key = (from_name, to_name)
            if key not in self._edges:
                msg = f'Graph "{self._name}": no registered edge {from_name} → {to_name}'
                raise ValueError(msg)
            self._edges.discard(key)

    def edges(self) -> frozenset[tuple[str, str]]:
        """Registered ``(from_name, to_name)`` pairs (read-only)."""
        with self._locked():
            return frozenset(self._edges)
