"""Graph container — GRAPHREFLY-SPEC §3.1–3.5 (Phase 1.1–1.2)."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from graphrefly.core.protocol import Messages, MessageType

if TYPE_CHECKING:
    from collections.abc import Iterator

    from graphrefly.core.node import NodeImpl

#: Separator for qualified paths (e.g. ``"parent::child::node"``).
PATH_SEP = "::"


class Graph:
    """Named registry of nodes with explicit edges (pure wires, no transforms).

    Qualified paths use ``::`` as the segment separator
    (e.g. ``"parent::child::node"``).

    When ``thread_safe`` is true (default), registry mutations serialize on an
    :class:`threading.RLock`. :meth:`remove` pops under that lock, then sends
    ``[[TEARDOWN]]`` outside the lock to avoid deadlocking against per-node
    subgraph write locks.

    :meth:`connect` is idempotent if the edge is already registered.
    :meth:`disconnect` removes a registered edge and raises :exc:`ValueError`
    if that edge was never recorded. It does not mutate ``NodeImpl`` dependency
    lists (see ``docs/optimizations.md``).

    **Composition (§3.4–3.5):** :meth:`mount` embeds a child graph; paths use
    ``::`` segments (e.g. ``parent::child::node``). :meth:`resolve` returns the
    node for a path. :meth:`signal` delivers messages to every node in this
    graph and recursively to mounted subgraphs.
    """

    __slots__ = ("_edges", "_lock", "_mounts", "_name", "_nodes", "_thread_safe")

    def __init__(self, name: str, opts: dict[str, Any] | None = None) -> None:
        """Create a graph. ``opts`` may include ``thread_safe`` (default ``True``)."""
        if not name:
            msg = "Graph name must be non-empty"
            raise ValueError(msg)
        if PATH_SEP in name:
            msg = f"Graph name must not contain {PATH_SEP!r} (got {name!r})"
            raise ValueError(msg)
        o = dict(opts or {})
        self._name = name
        self._thread_safe = bool(o.get("thread_safe", True))
        self._lock: threading.RLock | None = threading.RLock() if self._thread_safe else None
        self._nodes: dict[str, NodeImpl[Any]] = {}
        self._mounts: dict[str, Graph] = {}
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
        if PATH_SEP in node_name:
            msg = f"local node name must not contain {PATH_SEP!r} (path separator)"
            raise ValueError(msg)
        with self._locked():
            if node_name in self._mounts:
                raise KeyError(f"name {node_name!r} is already a mounted subgraph")
            if node_name in self._nodes:
                raise KeyError(f"duplicate node name: {node_name!r}")
            for existing_name, existing in self._nodes.items():
                if existing is n:
                    raise ValueError(f"node instance already registered as {existing_name!r}")
            if n._name is None:
                object.__setattr__(n, "_name", node_name)
            self._nodes[node_name] = n

    def mount(self, mount_name: str, child: Graph) -> None:
        """Embed ``child`` under ``mount_name`` (GRAPHREFLY-SPEC §3.4).

        Child nodes are addressable as ``f"{mount_name}::{local}"`` from this graph.
        Mount and top-level node names must not collide.
        """
        if not mount_name:
            raise ValueError("mount name must be non-empty")
        if PATH_SEP in mount_name:
            msg = f"mount name must not contain {PATH_SEP!r}"
            raise ValueError(msg)
        if child is self:
            raise ValueError("cannot mount a graph into itself")
        with self._locked():
            if mount_name in self._nodes:
                raise KeyError(f"name {mount_name!r} is already a registered node")
            if mount_name in self._mounts:
                raise KeyError(f"duplicate mount name: {mount_name!r}")
            for _m, g in self._mounts.items():
                if g is child:
                    raise ValueError("this child graph is already mounted here")
            if child._graph_reachable(self):
                raise ValueError("mount would create a cycle in the graph hierarchy")
            self._mounts[mount_name] = child

    def remove(self, node_name: str) -> None:
        """Unregister a node or unmount a subgraph; send ``[[TEARDOWN]]`` to affected nodes."""
        if PATH_SEP in node_name:
            msg = "remove() expects a single segment (local node or mount name on this graph)"
            raise ValueError(msg)
        with self._locked():
            if node_name in self._mounts:
                child = self._mounts.pop(node_name)
                self._edges = {
                    (f, t)
                    for f, t in self._edges
                    if not self._edge_touches_mount(f, t, node_name)
                }
            elif node_name in self._nodes:
                child = None
                n = self._nodes.pop(node_name)
                self._edges = {(f, t) for f, t in self._edges if f != node_name and t != node_name}
            else:
                raise KeyError(node_name)
        if child is not None:
            _teardown_mounted_graph(child)
            return
        n.down([(MessageType.TEARDOWN,)])

    def _edge_touches_mount(self, f: str, t: str, mount_name: str) -> bool:
        prefix = f"{mount_name}{PATH_SEP}"
        return f == mount_name or t == mount_name or f.startswith(prefix) or t.startswith(prefix)

    def resolve(self, path: str) -> NodeImpl[Any]:
        """Return the node for a ``::`` qualified path (GRAPHREFLY-SPEC §3.5).

        If the first segment equals this graph's :attr:`name`, it is stripped
        (so ``root.resolve("app::a")`` works when ``root.name == "app"``).
        """
        parts = path.split(PATH_SEP)
        if not parts or any(not p for p in parts):
            raise ValueError(f"path must be one or more non-empty {PATH_SEP!r}-separated segments")
        if parts[0] == self._name:
            parts = parts[1:]
            if not parts:
                raise ValueError(f"resolve path ends at graph name only: {path!r}")
        return self._resolve_parts_unlocked(parts, path)

    def _resolve_parts_unlocked(self, parts: list[str], path: str) -> NodeImpl[Any]:
        head, *tail = parts
        with self._locked():
            if head in self._nodes:
                if tail:
                    raise KeyError(f"{path!r}: {head!r} is a node, not a subgraph")
                return self._nodes[head]
            if head in self._mounts:
                if not tail:
                    raise KeyError(f"{path!r} names a subgraph, not a node")
                child = self._mounts[head]
            else:
                raise KeyError(path)
        return child._resolve_parts_unlocked(tail, path)

    def _resolve_endpoint(self, path: str) -> tuple[Graph, str, NodeImpl[Any]]:
        """Graph that owns the endpoint, local name in that graph, and node."""
        parts = path.split(PATH_SEP)
        if not parts or any(not p for p in parts):
            raise ValueError(f"path must be one or more non-empty {PATH_SEP!r}-separated segments")
        try:
            return self._resolve_endpoint_parts_unlocked(parts, path)
        except KeyError as e:
            raise KeyError(f"unknown node: {path!r}") from e

    def _resolve_endpoint_parts_unlocked(
        self, parts: list[str], path: str
    ) -> tuple[Graph, str, NodeImpl[Any]]:
        head, *tail = parts
        with self._locked():
            if head in self._nodes:
                if tail:
                    raise KeyError(f"{path!r}: {head!r} is a node, not a subgraph")
                return (self, head, self._nodes[head])
            if head in self._mounts:
                if not tail:
                    raise KeyError(f"{path!r} names a subgraph, not a node")
                child = self._mounts[head]
            else:
                raise KeyError(path)
        return child._resolve_endpoint_parts_unlocked(tail, path)

    def _graph_reachable(self, target: Graph) -> bool:
        if self is target:
            return True
        with self._locked():
            children = list(self._mounts.values())
        return any(c._graph_reachable(target) for c in children)

    def signal(self, messages: Messages) -> None:
        """Deliver ``messages`` to every node in this graph and mounted subgraphs (§3.4)."""
        _signal_graph(self, messages)

    def node(self, path: str) -> NodeImpl[Any]:
        """Return the node for a local name or a ``::`` qualified path."""
        if PATH_SEP in path:
            return self.resolve(path)
        with self._locked():
            try:
                return self._nodes[path]
            except KeyError as e:
                raise KeyError(path) from e

    def get(self, node_name: str) -> Any:
        """Shorthand for ``graph.node(name).get()`` — accepts ``::`` qualified paths."""
        return self.node(node_name).get()

    def set(self, node_name: str, value: Any) -> None:
        """Shorthand for ``graph.node(name).down([[DATA, value]])``.

        ``node_name`` accepts ``::`` qualified paths.
        """
        self.node(node_name).down([(MessageType.DATA, value)])

    def connect(self, from_path: str, to_path: str) -> None:
        """Record a pure wire; ``to`` must already list ``from`` as a constructor dependency.

        ``from_path`` and ``to_path`` are relative to this graph (local names or
        ``mount::...`` qualified paths).
        """
        if not from_path or not to_path:
            msg = "connect/disconnect paths must be non-empty"
            raise ValueError(msg)
        from_g, from_local, from_n = self._resolve_endpoint(from_path)
        to_g, to_local, to_n = self._resolve_endpoint(to_path)
        if from_n is to_n:
            raise ValueError("cannot connect a node to itself")
        if not any(d is from_n for d in to_n._deps):
            raise ValueError(
                f"connect({from_path!r}, {to_path!r}): target must include the source "
                "node in its dependency list at construction (pure wire)"
            )
        if from_g is to_g:
            from_g._connect_local(from_local, to_local)
        else:
            key = (from_path, to_path)
            with self._locked():
                if key in self._edges:
                    return
                self._edges.add(key)

    def _connect_local(self, from_name: str, to_name: str) -> None:
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

    def disconnect(self, from_path: str, to_path: str) -> None:
        """Remove a registered edge (bookkeeping only; see class docstring)."""
        if not from_path or not to_path:
            msg = "connect/disconnect paths must be non-empty"
            raise ValueError(msg)
        from_g, from_local, _f = self._resolve_endpoint(from_path)
        to_g, to_local, _t = self._resolve_endpoint(to_path)
        if from_g is to_g:
            from_g._disconnect_local(from_local, to_local)
        else:
            key = (from_path, to_path)
            with self._locked():
                if key not in self._edges:
                    msg = f'Graph "{self._name}": no registered edge {from_path} → {to_path}'
                    raise ValueError(msg)
                self._edges.discard(key)

    def _disconnect_local(self, from_name: str, to_name: str) -> None:
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


def _teardown_mounted_graph(root: Graph) -> None:
    with root._locked():
        mounts = list(root._mounts.values())
    for m in mounts:
        _teardown_mounted_graph(m)
    with root._locked():
        nodes = list(root._nodes.values())
    for n in nodes:
        n.down([(MessageType.TEARDOWN,)])


def _signal_graph(g: Graph, messages: Messages) -> None:
    with g._locked():
        mounts = list(g._mounts.values())
        nodes = list(g._nodes.values())
    for m in mounts:
        _signal_graph(m, messages)
    for n in nodes:
        n.down(messages)
