"""Graph container — GRAPHREFLY-SPEC §3.1–3.8 (Phase 1.1–1.4)."""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import threading
from collections import deque
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, cast

from graphrefly.core.clock import monotonic_ns
from graphrefly.core.guard import GuardDenied, normalize_actor
from graphrefly.core.meta import DescribeDetail, describe_node, resolve_describe_fields
from graphrefly.core.node import NodeImpl
from graphrefly.core.protocol import Messages, MessageType, is_batching, message_tier
from graphrefly.core.sugar import state

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class DescribeResult(dict[str, Any]):
    """Dict subclass returned by :meth:`Graph.describe`.

    Provides an ``expand()`` method for re-reading the live graph at a higher
    detail level.  Unlike storing expand as a dict key, this is safe for
    ``json.dumps(graph.describe())`` — the method is not serialised.
    """

    def expand(self, detail_or_fields: Any = None) -> DescribeResult:
        """Re-read the live graph at a higher detail level or with explicit fields."""
        fn = object.__getattribute__(self, "_expand_fn")
        return cast("DescribeResult", fn(detail_or_fields))


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """Single entry in the :meth:`Graph.trace_log` ring buffer."""

    timestamp_ns: int
    path: str
    reason: str


@dataclass(slots=True)
class ObserveResult:
    """Structured observation result from :meth:`Graph.observe` with ``structured=True``.

    Accumulates events from the observation and provides a ``dispose()`` method
    to unsubscribe.
    """

    values: dict[str, Any] = field(default_factory=dict)
    dirty_count: int = 0
    resolved_count: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    completed_cleanly: bool = False
    errored: bool = False
    _dispose_fn: Callable[[], None] | None = field(default=None, repr=False)
    _graph: Any = field(default=None, repr=False)
    _path: str | None = field(default=None, repr=False)
    _observe_opts: dict[str, Any] = field(default_factory=dict, repr=False)

    def dispose(self) -> None:
        """Unsubscribe from the observation."""
        if self._dispose_fn is not None:
            self._dispose_fn()
            self._dispose_fn = None

    def expand(self, extra: Any = None) -> ObserveResult:
        """Resubscribe with higher detail. Disposes current, returns new ObserveResult.

        Args:
            extra: Either a detail string (``"full"``, ``"standard"``, ``"minimal"``)
                or a dict of options like ``{"causal": True, "timeline": True}``.
        """
        self.dispose()
        if self._graph is None:
            raise RuntimeError("Cannot expand: no graph reference stored")
        opts = dict(self._observe_opts)
        if isinstance(extra, str):
            opts["detail"] = extra
        elif isinstance(extra, dict):
            opts.update(extra)
        else:
            opts["detail"] = "full"
        result = self._graph.observe(self._path, **opts)
        if not isinstance(result, ObserveResult):
            raise TypeError("expand requires structured observe mode")
        return result


@dataclass(slots=True)
class SpyHandle:
    """Handle returned by :meth:`Graph.spy` (TS parity).

    Exposes the structured accumulator as ``result`` and a top-level ``dispose()``.
    """

    result: ObserveResult

    def dispose(self) -> None:
        self.result.dispose()


@dataclass(frozen=True, slots=True)
class GraphDiffNodeChange:
    """A single field change detected by :meth:`Graph.diff`."""

    path: str
    field: str
    from_value: Any
    to_value: Any


@dataclass(frozen=True, slots=True)
class GraphDiffEdge:
    """An edge entry in :class:`GraphDiffResult`."""

    from_node: str
    to_node: str


@dataclass(frozen=True, slots=True)
class GraphDiffResult:
    """Result of :meth:`Graph.diff` comparing two describe outputs."""

    nodesAdded: list[str] = field(default_factory=list)
    nodesRemoved: list[str] = field(default_factory=list)
    nodesChanged: list[GraphDiffNodeChange] = field(default_factory=list)
    edgesAdded: list[GraphDiffEdge] = field(default_factory=list)
    edgesRemoved: list[GraphDiffEdge] = field(default_factory=list)
    subgraphsAdded: list[str] = field(default_factory=list)
    subgraphsRemoved: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class GraphAutoCheckpointHandle:
    dispose: Callable[[], None]


#: Separator for qualified paths (e.g. ``"parent::child::node"``).
PATH_SEP = "::"

#: Reserved path segment for companion meta nodes (``parent::__meta__::field``).
#: Local node and mount names must not equal this string.
GRAPH_META_SEGMENT = "__meta__"

#: Backward-compat alias — prefer :data:`GRAPH_META_SEGMENT`.
META_PATH_SEG = GRAPH_META_SEGMENT

#: Snapshot envelope version for :meth:`Graph.snapshot` / :meth:`Graph.restore` /
#: :meth:`Graph.from_snapshot` (GRAPHREFLY-SPEC §3.8).
GRAPH_SNAPSHOT_VERSION = 1

_GRAPH_DIAGRAM_DIRECTIONS = frozenset({"TD", "LR", "BT", "RL"})

#: Sentinel: :meth:`Graph.describe` without ``actor`` returns the full graph (backward compat).
_DESCRIBE_UNSCOPED = object()


def _node_allows_observe(n: NodeImpl[Any], actor: dict[str, Any]) -> bool:
    return n.allows_observe(actor)


def _parse_snapshot_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Validate flat snapshot envelope (``version`` field) and return ``data``."""
    ver = data.get("version")
    if ver != GRAPH_SNAPSHOT_VERSION:
        msg = f"unsupported snapshot version {ver!r} (expected {GRAPH_SNAPSHOT_VERSION})"
        raise ValueError(msg)
    for key in ("name", "nodes", "edges", "subgraphs"):
        if key not in data:
            msg = f"snapshot missing required key {key!r}"
            raise ValueError(msg)
    if not isinstance(data["name"], str):
        msg = f"snapshot 'name' must be a str, got {type(data['name']).__name__}"
        raise TypeError(msg)
    if not isinstance(data["nodes"], dict):
        msg = f"snapshot 'nodes' must be a dict, got {type(data['nodes']).__name__}"
        raise TypeError(msg)
    if not isinstance(data["edges"], list):
        msg = f"snapshot 'edges' must be a list, got {type(data['edges']).__name__}"
        raise TypeError(msg)
    if not isinstance(data["subgraphs"], list):
        msg = f"snapshot 'subgraphs' must be a list, got {type(data['subgraphs']).__name__}"
        raise TypeError(msg)
    return data


def _path_has_meta_segment(path: str) -> bool:
    return GRAPH_META_SEGMENT in path.split(PATH_SEP)


def _normalize_diagram_direction(direction: str | None) -> str:
    if direction is None:
        return "LR"
    if direction not in _GRAPH_DIAGRAM_DIRECTIONS:
        valid = ", ".join(sorted(_GRAPH_DIAGRAM_DIRECTIONS))
        raise ValueError(f"invalid diagram direction {direction!r}; expected one of: {valid}")
    return direction


def _d2_direction_from_graph_direction(direction: str) -> str:
    if direction == "TD":
        return "down"
    if direction == "BT":
        return "up"
    if direction == "RL":
        return "left"
    return "right"


def _escape_mermaid_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _escape_d2_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


_SPY_ANSI_THEME: dict[str, str] = {
    "data": "\u001b[32m",
    "dirty": "\u001b[33m",
    "resolved": "\u001b[36m",
    "complete": "\u001b[34m",
    "error": "\u001b[31m",
    "derived": "\u001b[35m",
    "path": "\u001b[90m",
    "reset": "\u001b[0m",
}
_SPY_NO_COLOR_THEME: dict[str, str] = {
    "data": "",
    "dirty": "",
    "resolved": "",
    "complete": "",
    "error": "",
    "derived": "",
    "path": "",
    "reset": "",
}


def _resolve_spy_theme(theme: str | dict[str, str] | None) -> dict[str, str]:
    if theme in (None, "ansi"):
        return dict(_SPY_ANSI_THEME)
    if theme == "none":
        return dict(_SPY_NO_COLOR_THEME)
    if isinstance(theme, dict):
        out = dict(_SPY_NO_COLOR_THEME)
        for key, value in theme.items():
            if key in out and isinstance(value, str):
                out[key] = value
        return out
    return dict(_SPY_NO_COLOR_THEME)


def _message_type_label(msg_type: Any) -> str | None:
    if msg_type is MessageType.DATA:
        return "data"
    if msg_type is MessageType.DIRTY:
        return "dirty"
    if msg_type is MessageType.RESOLVED:
        return "resolved"
    if msg_type is MessageType.COMPLETE:
        return "complete"
    if msg_type is MessageType.ERROR:
        return "error"
    return None


def _describe_data(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool | int | float) or value is None:
        return str(value)
    try:
        return json.dumps(value)
    except Exception:
        return "[unserializable]"


def _clear_graph_registry(root: Graph) -> None:
    """Remove all mounts, nodes, and edges without sending messages (after TEARDOWN)."""
    with root._locked():
        mounts = list(root._mounts.items())
        root._mounts.clear()
        root._nodes.clear()
        root._edges.clear()
    for _mn, ch in mounts:
        _clear_graph_registry(ch)


def _ensure_qualified_mount(root: Graph, qualified: str) -> None:
    """Create ``mount::...`` chain on ``root`` if missing (``from_snapshot`` helper)."""
    parts = qualified.split(PATH_SEP)
    g = root
    for seg in parts:
        with g._locked():
            in_mounts = seg in g._mounts
            in_nodes = seg in g._nodes
        if in_nodes:
            msg = f"snapshot mount path {qualified!r} collides with existing node {seg!r}"
            raise ValueError(msg)
        if not in_mounts:
            g.mount(seg, Graph(seg))
        with g._locked():
            g = g._mounts[seg]


def _owner_graph_and_local(root: Graph, path: str) -> tuple[Graph, str]:
    parts = path.split(PATH_SEP)
    if not parts or any(not p for p in parts):
        raise ValueError(f"invalid path {path!r}")
    if _path_has_meta_segment(path):
        msg = f"expected primary node path without {GRAPH_META_SEGMENT!r} segment, got {path!r}"
        raise ValueError(msg)
    *mounts, local = parts
    g = root
    for seg in mounts:
        with g._locked():
            try:
                g = g._mounts[seg]
            except KeyError as e:
                raise KeyError(f"unknown mount {seg!r} in path {path!r}") from e
    return g, local


class Graph:
    """Named registry of nodes with explicit edges (pure wires, no transforms).

    Qualified paths use ``::`` as the segment separator
    (e.g. ``"parent::child::node"``). Registry mutations are serialized on an
    :class:`threading.RLock` when ``thread_safe`` is ``True`` (default).

    :meth:`connect` is idempotent; :meth:`disconnect` raises :exc:`ValueError`
    if the edge was never registered. Neither method mutates ``NodeImpl``
    dependency lists.

    :meth:`mount` embeds a child graph (GRAPHREFLY-SPEC §3.4–3.5).
    :meth:`signal` broadcasts to every registered node, its meta companions, and
    all mounted subgraphs. :meth:`describe` returns Appendix-B-shaped JSON;
    :meth:`observe` exposes a live message stream.

    Args:
        name: Registry name used in ``describe()`` output and diagnostics.
        opts: Optional dict with keys ``thread_safe`` (bool, default ``True``)
            and ``trace_size`` (int ring-buffer size for :meth:`annotate`).

    Example:
        ```python
        from graphrefly import Graph, state
        g = Graph("demo")
        x = state(0, name="x")
        y = state(0, name="y")
        g.add("x", x)
        g.add("y", y)
        g.connect("x", "y")
        g.set("x", 1)
        assert g.get("y") == 1
        ```
    """

    inspector_enabled: ClassVar[bool] = os.environ.get("NODE_ENV") != "production"
    _factories: ClassVar[list[tuple[str, Callable[[str, dict[str, Any]], NodeImpl[Any]]]]] = []

    __slots__ = (
        "_annotations",
        "_auto_checkpoint_disposers",
        "_default_versioning_level",
        "_edges",
        "_lock",
        "_mounts",
        "_name",
        "_nodes",
        "_thread_safe",
        "_trace_ring",
    )

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
        self._annotations: dict[str, str] = {}
        self._trace_ring: deque[TraceEntry] = deque(maxlen=1000)
        self._auto_checkpoint_disposers: set[Callable[[], None]] = set()
        self._default_versioning_level: int | None = None

    @property
    def name(self) -> str:
        return self._name

    @classmethod
    def register_factory(
        cls, pattern: str, factory: Callable[[str, dict[str, Any]], NodeImpl[Any]]
    ) -> None:
        if not pattern:
            raise ValueError("Graph.register_factory requires a non-empty pattern")
        cls.unregister_factory(pattern)
        cls._factories.append((pattern, factory))

    @classmethod
    def unregister_factory(cls, pattern: str) -> None:
        cls._factories = [entry for entry in cls._factories if entry[0] != pattern]

    @classmethod
    def _factory_for_path(cls, path: str) -> Callable[[str, dict[str, Any]], NodeImpl[Any]] | None:
        for pattern, factory in reversed(cls._factories):
            if fnmatch.fnmatchcase(path, pattern):
                return factory
        return None

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock = self._lock
        if lock is not None:
            with lock:
                yield
        else:
            yield

    def add(self, node_name: str, n: NodeImpl[Any]) -> None:
        """Register a node under the given name in this graph.

        Sets ``n``'s internal name to ``node_name`` when the node has no name set.
        Raises :exc:`ValueError` if ``node_name`` is already registered.

        Args:
            node_name: Local name (no ``::`` separators).
            n: The :class:`~graphrefly.core.node.NodeImpl` to register.

        Example:
            ```python
            from graphrefly import Graph, state
            g = Graph("g")
            g.add("x", state(0))
            ```
        """
        if not node_name:
            raise ValueError("node name must be non-empty")
        if node_name == GRAPH_META_SEGMENT:
            msg = f"reserved name {GRAPH_META_SEGMENT!r} cannot be used as a local node name"
            raise ValueError(msg)
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
            if self._default_versioning_level is not None:
                n._apply_versioning(self._default_versioning_level)
            self._nodes[node_name] = n
            # Auto-register edges from constructor deps already in this graph.
            # Forward: this node's deps → already-registered nodes.
            for dep in n._deps:
                for existing_name, existing in self._nodes.items():
                    if existing is dep:
                        self._edges.add((existing_name, node_name))
                        break
            # Reverse: already-registered nodes that depend on this newly added node.
            for other_name, other_node in self._nodes.items():
                if other_name == node_name:
                    continue
                if n in other_node._deps:
                    self._edges.add((node_name, other_name))

    def set_versioning(self, level: int | None) -> None:
        """Set default versioning level for all nodes in this graph (roadmap §6.0).

        Retroactively upgrades already-registered nodes. Nodes added later via
        :meth:`add` inherit this level unless they already have versioning.

        **Scope:** Does not propagate to mounted subgraphs. Call
        ``set_versioning`` on each child graph separately if needed.

        Args:
            level: ``0`` for V0, ``1`` for V1, or ``None`` to clear.
        """
        self._default_versioning_level = level
        if level is None:
            return
        with self._locked():
            for n in self._nodes.values():
                n._apply_versioning(level)

    def mount(self, mount_name: str, child: Graph) -> None:
        """Embed a child graph under ``mount_name`` (GRAPHREFLY-SPEC §3.4).

        Child nodes become addressable as ``"mount_name::local_name"`` from this
        graph. Mount and top-level node names must not collide.

        Args:
            mount_name: Local name for the mount point (no ``::`` separators).
            child: The :class:`Graph` to embed.

        Example:
            ```python
            from graphrefly import Graph, state
            parent = Graph("parent")
            child = Graph("child")
            child.add("v", state(1))
            parent.mount("sub", child)
            assert parent.get("sub::v") == 1
            ```
        """
        if not mount_name:
            raise ValueError("mount name must be non-empty")
        if PATH_SEP in mount_name:
            msg = f"mount name must not contain {PATH_SEP!r}"
            raise ValueError(msg)
        if child is self:
            raise ValueError("cannot mount a graph into itself")
        if mount_name == GRAPH_META_SEGMENT:
            msg = f"reserved name {GRAPH_META_SEGMENT!r} cannot be used as a mount name"
            raise ValueError(msg)
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
        """Unregister a node or unmount a subgraph and send ``[[TEARDOWN]]`` to it.

        Removes the name from the internal registry and sends teardown outside the
        lock to avoid deadlocking against per-node subgraph write locks.

        Args:
            node_name: Local name of a registered node or mount point.

        Example:
            ```python
            from graphrefly import Graph, state
            g = Graph("g")
            g.add("x", state(0))
            g.remove("x")
            ```
        """
        if PATH_SEP in node_name:
            msg = "remove() expects a single segment (local node or mount name on this graph)"
            raise ValueError(msg)
        with self._locked():
            if node_name in self._mounts:
                child = self._mounts.pop(node_name)
                self._edges = {
                    (f, t) for f, t in self._edges if not self._edge_touches_mount(f, t, node_name)
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
        # Registry already dropped this name — teardown must not fail on guards (orphan risk).
        n.down([(MessageType.TEARDOWN,)], internal=True)

    def _edge_touches_mount(self, f: str, t: str, mount_name: str) -> bool:
        prefix = f"{mount_name}{PATH_SEP}"
        return f == mount_name or t == mount_name or f.startswith(prefix) or t.startswith(prefix)

    def resolve(self, path: str) -> NodeImpl[Any]:
        """Return the node at a ``::``-qualified path (GRAPHREFLY-SPEC §3.5).

        Traverses mounts as needed; handles ``__meta__`` segments for companion
        nodes. If the first segment matches this graph's name it is stripped.
        Raises :exc:`KeyError` for unknown path segments.

        Args:
            path: Fully-qualified path (e.g. ``"parent::child::node"``).

        Returns:
            The :class:`~graphrefly.core.node.NodeImpl` at that path.

        Example:
            ```python
            from graphrefly import Graph, state
            g = Graph("g")
            g.add("x", state(42))
            assert g.resolve("x").get() == 42
            ```
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
                base = self._nodes[head]
                if not tail:
                    return base
                return _finish_resolve_from_node(base, tail, path)
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
        if GRAPH_META_SEGMENT in parts:
            msg = (
                "connect/disconnect endpoints must be registered graph nodes, "
                f"not meta paths (got {path!r})"
            )
            raise ValueError(msg)
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

    def signal(
        self,
        messages: Messages,
        *,
        actor: Any | None = None,
        internal: bool = False,
    ) -> None:
        """Deliver messages to every registered node, meta companion, and mounted subgraph.

        When a node has a guard, it is checked with action ``"signal"``; nodes that
        reject it are silently skipped.

        Args:
            messages: The :class:`~graphrefly.core.protocol.Messages` to broadcast.
            actor: Optional actor context checked against each node's guard.

        Example:
            ```python
            from graphrefly import Graph, state
            from graphrefly.core.protocol import MessageType
            g = Graph("g")
            g.add("x", state(0))
            g.signal([(MessageType.INVALIDATE,)])
            ```
        """
        _signal_graph(self, messages, set(), normalize_actor(actor), "", internal=internal)

    def destroy(self) -> None:
        """Teardown all nodes and clear every registry in this graph and its mounts.

        Sends ``[[TEARDOWN]]`` to every node in the same visitation order as
        :meth:`signal` (GRAPHREFLY-SPEC §3.7), then recursively clears mounted subgraphs.

        Example:
            ```python
            from graphrefly import Graph, state
            g = Graph("g")
            g.add("x", state(0))
            g.destroy()
            ```
        """
        _signal_graph(
            self,
            [(MessageType.TEARDOWN,)],
            set(),
            normalize_actor(None),
            "",
            internal=True,
        )
        for dispose in list(self._auto_checkpoint_disposers):
            with suppress(Exception):
                dispose()
        self._auto_checkpoint_disposers.clear()
        _clear_graph_registry(self)

    def snapshot(self) -> dict[str, Any]:
        """Serialize graph structure and current node values to a JSON-friendly dict (§3.8).

        Wraps :meth:`describe` output in a versioned envelope required by
        :meth:`restore` / :meth:`from_snapshot`. The format is stable for
        :meth:`to_json_string` determinism.

        Returns:
            A ``dict`` with keys ``version``, ``name``, ``nodes``, ``edges``,
            and ``subgraphs``.

        Example:
            ```python
            from graphrefly import Graph, state
            g = Graph("g")
            g.add("x", state(42))
            snap = g.snapshot()
            assert snap["version"] == 1
            ```
        """
        raw = self.describe(detail="full")
        # Strip non-restorable fields (runtime attribution) so snapshot → restore → snapshot
        # is idempotent. Use describe(detail="full") for audit snapshots instead.
        nodes_sorted = dict(
            sorted(
                (p, {k: v for k, v in nd.items() if k not in ("last_mutation", "guard")})
                for p, nd in raw["nodes"].items()
            )
        )
        subgraphs_sorted = sorted(raw["subgraphs"])
        return {
            "version": GRAPH_SNAPSHOT_VERSION,
            "name": raw["name"],
            "nodes": nodes_sorted,
            "edges": raw["edges"],
            "subgraphs": subgraphs_sorted,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return the current snapshot as a plain dict with sorted keys (§3.8).

        Equivalent to :meth:`snapshot`. Use :meth:`to_json_string` for
        deterministic text.
        """
        return self.snapshot()

    def to_json_string(self) -> str:
        """Return deterministic JSON text for the current :meth:`snapshot` (§3.8).

        Produces compact, sorted-key JSON with a trailing newline (suitable for
        version control). Raises :exc:`TypeError` when a node value is not
        JSON-serializable.

        Returns:
            A compact JSON ``str`` ending with a newline.

        Example:
            ```python
            from graphrefly import Graph, state
            g = Graph("g")
            g.add("x", state(0))
            assert g.to_json_string().startswith("{")
            ```
        """
        return (
            json.dumps(
                self.snapshot(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )

    def auto_checkpoint(
        self,
        adapter: Any,
        *,
        debounce_ms: int = 500,
        compact_every: int = 10,
        filter: Callable[[str, dict[str, Any]], bool] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> GraphAutoCheckpointHandle:
        """Arm debounced reactive persistence via graph-wide observe stream.

        Trigger gate uses :func:`message_tier`: only message batches containing
        tier >= 2 tuples schedule a checkpoint.
        """

        lock = threading.Lock()
        timer: threading.Timer | None = None
        seq = 0
        pending = False
        last_describe: dict[str, Any] | None = None
        debounce_s = max(0.0, float(debounce_ms) / 1000.0)
        compact_every = max(1, int(compact_every))

        def flush() -> None:
            nonlocal timer, seq, pending, last_describe
            with lock:
                timer = None
                if not pending:
                    return
                pending = False
            try:
                raw = self.describe(detail="full")
                # Strip non-restorable fields for persistence idempotency
                clean_nodes = {
                    p: {k: v for k, v in nd.items() if k not in ("last_mutation", "guard")}
                    for p, nd in raw["nodes"].items()
                }
                described = {**raw, "nodes": clean_nodes}
                snapshot = {**described, "version": GRAPH_SNAPSHOT_VERSION}
                seq += 1
                if last_describe is None or seq % compact_every == 0:
                    adapter.save(self.name, {"mode": "full", "snapshot": snapshot, "seq": seq})
                else:
                    diff = Graph.diff(last_describe, described)
                    adapter.save(
                        self.name,
                        {"mode": "diff", "diff": diff.__dict__, "snapshot": snapshot, "seq": seq},
                    )
                last_describe = described
            except Exception as exc:
                if on_error is not None:
                    on_error(exc)

        def schedule() -> None:
            nonlocal timer, pending
            with lock:
                pending = True
                if timer is not None:
                    timer.cancel()
                timer = threading.Timer(debounce_s, flush)
                timer.daemon = True
                timer.start()

        def on_msgs(path: str, msgs: Messages) -> None:
            if not any(message_tier(m[0]) >= 2 for m in msgs):
                return
            if filter is not None:
                nd = self.resolve(path)
                if nd is None:
                    return
                node_desc = describe_node(nd, resolve_describe_fields("standard"))
                if not filter(path, node_desc):
                    return
            schedule()

        observe_stream = self.observe()
        if not isinstance(observe_stream, GraphObserveSource):
            raise TypeError("auto_checkpoint expected GraphObserveSource from observe()")
        unsub = observe_stream.subscribe(on_msgs)

        def dispose() -> None:
            nonlocal timer
            unsub()
            with lock:
                if timer is not None:
                    timer.cancel()
                    timer = None
            self._auto_checkpoint_disposers.discard(dispose)

        self._auto_checkpoint_disposers.add(dispose)
        return GraphAutoCheckpointHandle(dispose=dispose)

    def restore(self, data: dict[str, Any], *, only: str | list[str] | None = None) -> None:
        """Apply ``value`` fields from a prior :meth:`snapshot` onto this graph (§3.8).

        Only ``state`` and ``producer`` entries with a ``value`` key are written;
        derived/operator nodes recompute from the restored sources.
        Raises :exc:`ValueError` on snapshot version mismatch.

        Args:
            data: A snapshot dict previously produced by :meth:`snapshot`.

        Example:
            ```python
            from graphrefly import Graph, state
            g = Graph("g")
            x = state(0)
            g.add("x", x)
            snap = g.snapshot()
            x.down([("DATA", 99)])
            g.restore(snap)
            assert g.get("x") == 0
            ```
        """
        _parse_snapshot_envelope(data)
        if data["name"] != self._name:
            msg = (
                f'Graph "{self._name}": restore snapshot name '
                f'"{data["name"]}" does not match this graph'
            )
            raise ValueError(msg)
        only_patterns = None if only is None else ([only] if isinstance(only, str) else list(only))
        for path in sorted(data["nodes"]):
            if only_patterns is not None and not any(
                fnmatch.fnmatchcase(path, p) for p in only_patterns
            ):
                continue
            spec = data["nodes"][path]
            if not isinstance(spec, dict) or "value" not in spec:
                continue
            ntype = spec.get("type")
            if ntype in ("derived", "operator", "effect"):
                continue
            with contextlib.suppress(KeyError, ValueError):
                self.set(path, spec["value"])

    @classmethod
    def from_snapshot(
        cls,
        data: dict[str, Any],
        build: Any | None = None,
    ) -> Graph:
        """Build a new graph from :meth:`snapshot` data (§3.8).

        When *build* is provided it is called with the empty graph first, letting
        callers register derived nodes before :meth:`restore` is applied. State
        nodes not pre-registered are created automatically.

        Args:
            data: A snapshot dict produced by :meth:`snapshot`.
            build: Optional callable ``(graph) -> None`` to pre-register derived nodes.

        Returns:
            A new :class:`Graph` with state nodes restored from ``data``.

        Example:
            ```python
            from graphrefly import Graph, state
            g = Graph("g")
            g.add("x", state(42))
            snap = g.snapshot()
            g2 = Graph.from_snapshot(snap)
            assert g2.get("x") == 42
            ```
        """
        _parse_snapshot_envelope(data)
        root = cls(data["name"])
        if build is not None:
            build(root)
            root.restore(data)
            return root
        for q in sorted(data["subgraphs"], key=lambda p: (p.count(PATH_SEP), p)):
            _ensure_qualified_mount(root, q)
        primary_paths = [
            p
            for p in sorted(data["nodes"])
            if not _path_has_meta_segment(p) and isinstance(data["nodes"][p], dict)
        ]
        pending: dict[str, dict[str, Any]] = {p: data["nodes"][p] for p in primary_paths}
        created: dict[str, NodeImpl[Any]] = {}
        progressed = True
        while pending and progressed:
            progressed = False
            for path in list(sorted(pending)):
                spec = pending[path]
                deps = spec.get("deps")
                dep_paths = (
                    [d for d in deps if isinstance(d, str)] if isinstance(deps, list) else []
                )
                if not all(dep in created for dep in dep_paths):
                    continue
                owner, local = _owner_graph_and_local(root, path)
                meta_plain = spec.get("meta")
                meta_kw: dict[str, Any] = dict(meta_plain) if isinstance(meta_plain, dict) else {}
                ntype = spec.get("type", "state")
                if ntype == "state":
                    new_node = state(spec.get("value"), meta=meta_kw)
                else:
                    factory = cls._factory_for_path(path)
                    if factory is None:
                        continue
                    new_node = factory(
                        local,
                        {
                            "path": path,
                            "type": ntype,
                            "value": spec.get("value"),
                            "meta": meta_kw,
                            "deps": dep_paths,
                            "resolved_deps": [created[d] for d in dep_paths],
                        },
                    )
                with owner._locked():
                    if local in owner._nodes or local in owner._mounts:
                        msg = f"snapshot path {path!r} collides with an existing name"
                        raise ValueError(msg)
                owner.add(local, new_node)
                created[path] = new_node
                pending.pop(path, None)
                progressed = True
        if pending:
            unresolved = ", ".join(sorted(pending))
            raise ValueError(
                "Graph.from_snapshot could not reconstruct nodes without build callback: "
                f"{unresolved}. Register factories with Graph.register_factory(pattern, factory)."
            )
        for edge in data["edges"]:
            if not isinstance(edge, dict):
                continue
            ef = edge.get("from")
            et = edge.get("to")
            if not isinstance(ef, str) or not isinstance(et, str):
                continue
            with suppress(Exception):
                root.connect(ef, et)
        root.restore(data)
        return root

    def describe(
        self,
        *,
        actor: Any = _DESCRIBE_UNSCOPED,
        filter: dict[str, Any] | Callable[..., bool] | None = None,
        detail: str | None = None,
        fields: list[str] | None = None,
        format: str | None = None,
    ) -> DescribeResult:
        """Static structure snapshot (GRAPHREFLY-SPEC §3.6, Appendix B).

        ``nodes`` keys are qualified paths (including ``::__meta__::`` for companions).
        ``edges`` use the same qualified naming. ``subgraphs`` lists every mount point
        in the hierarchy with paths from this graph's root.

        With ``actor=...``, only nodes the actor may observe are included; **edges** whose
        ``from`` or ``to`` is hidden are dropped (roadmap 1.5 D). **Subgraphs** are kept
        only if at least one visible node path lies under that mount prefix.
        Omitting ``actor`` preserves the previous unfiltered behavior.

        With ``filter=...``, further restrict the output:

        - If ``filter`` is a ``dict``, each key is matched against node description fields
          (e.g. ``{"type": "state"}`` keeps only nodes whose ``type`` is ``"state"``).
        - If ``filter`` is a callable, it receives ``(path, node_desc)`` and must return
          ``True`` to include the node.

        .. note::
           Filters operate on whatever fields the chosen ``detail`` provides.
           For ``meta_has`` and ``status`` filters, use ``detail="standard"`` or
           higher — at ``"minimal"`` those fields are absent and the filter
           silently excludes all nodes.

        Progressive disclosure via ``detail`` / ``fields`` / ``format``:

        - ``detail="minimal"`` (default): only ``type`` and ``deps``
        - ``detail="standard"``: ``type``, ``status``, ``value``, ``deps``, ``meta``, ``v``
        - ``detail="full"``: standard + ``guard``, ``last_mutation``
        - ``fields=[...]``: explicit field list (overrides ``detail``)
        - ``format="spec"``: force minimal (type + deps only)

        Returns a :class:`DescribeResult` (a ``dict`` subclass) with an
        ``expand()`` method that re-reads the live graph with higher detail,
        preserving actor/filter from the original call.  Because ``expand`` is a
        method (not a dict key), ``json.dumps(graph.describe())`` works safely.

        Args:
            actor: Optional actor for guard-based filtering.
            filter: Optional dict or predicate to filter nodes in the output.
            detail: Progressive disclosure level: ``"minimal"``, ``"standard"``, or ``"full"``.
            fields: Explicit list of fields to include (overrides ``detail``).
            format: ``"spec"`` forces minimal fields.
        """
        # Resolve include_fields from detail/fields/format
        if format == "spec":
            include_fields: set[str] | None = {"type", "deps"}
        else:
            include_fields = resolve_describe_fields(cast("DescribeDetail | None", detail), fields)

        targets = _collect_observe_targets(self, "")
        paths_by_id = {id(n): p for p, n in targets}
        nodes_out = {
            p: _node_describe_for_graph(n, paths_by_id, include_fields) for p, n in targets
        }
        edges_out = _collect_edges_qualified(self, "")
        edges_out.sort(key=lambda e: (e["from"], e["to"]))
        subgraphs_out = _collect_subgraphs_qualified(self, "")
        nodes_map: dict[str, Any] = nodes_out
        raw: dict[str, Any] = {
            "name": self._name,
            "nodes": nodes_map,
            "edges": edges_out,
            "subgraphs": subgraphs_out,
        }
        if actor is not _DESCRIBE_UNSCOPED:
            a = normalize_actor(actor)
            visible = {p for p, n in targets if _node_allows_observe(n, a)}
            nodes_map = {k: v for k, v in nodes_map.items() if k in visible}
            edges_out = [e for e in edges_out if e["from"] in visible and e["to"] in visible]
            sep = PATH_SEP
            subgraphs_out = [
                s
                for s in subgraphs_out
                if any(p == s or p.startswith(f"{s}{sep}") for p in visible)
            ]
            raw = {**raw, "nodes": nodes_map, "edges": edges_out, "subgraphs": subgraphs_out}
        if filter is not None:
            if callable(filter):
                nodes_map = {p: d for p, d in raw["nodes"].items() if filter(p, d)}
            elif isinstance(filter, dict):

                def _match(desc: dict[str, Any]) -> bool:
                    for k, v in filter.items():
                        if k in ("deps_includes", "depsIncludes"):
                            deps = desc.get("deps")
                            if not isinstance(deps, list) or str(v) not in deps:
                                return False
                            continue
                        if k in ("meta_has", "metaHas"):
                            meta = desc.get("meta", {})
                            if not isinstance(meta, dict) or str(v) not in meta:
                                return False
                            continue
                        if desc.get(k) != v:
                            return False
                    return True

                nodes_map = {p: d for p, d in raw["nodes"].items() if _match(d)}
            visible_after_filter = set(nodes_map.keys())
            edges_out = [
                e
                for e in raw["edges"]
                if e["from"] in visible_after_filter and e["to"] in visible_after_filter
            ]
            sep = PATH_SEP
            subgraphs_out = [
                s
                for s in raw["subgraphs"]
                if any(p == s or p.startswith(f"{s}{sep}") for p in visible_after_filter)
            ]
            raw = {**raw, "nodes": nodes_map, "edges": edges_out, "subgraphs": subgraphs_out}

        # expand helper — re-reads the live graph with higher detail
        _captured_actor = actor
        _captured_filter = filter

        def _expand(detail_or_fields: Any = None) -> DescribeResult:
            kw: dict[str, Any] = {}
            if _captured_actor is not _DESCRIBE_UNSCOPED:
                kw["actor"] = _captured_actor
            if _captured_filter is not None:
                kw["filter"] = _captured_filter
            if isinstance(detail_or_fields, str):
                kw["detail"] = detail_or_fields
            elif isinstance(detail_or_fields, list):
                kw["fields"] = detail_or_fields
            else:
                kw["detail"] = "full"
            return self.describe(**kw)

        result = DescribeResult(raw)
        object.__setattr__(result, "_expand_fn", _expand)
        return result

    def observe(
        self,
        path: str | None = None,
        *,
        actor: Any | None = None,
        structured: bool = False,
        timeline: bool = False,
        causal: bool = False,
        derived: bool = False,
        detail: str | None = None,
    ) -> GraphObserveSource | ObserveResult:
        """Live message stream for one node (and its path) or the whole graph (§3.6).

        Use :meth:`GraphObserveSource.subscribe` to attach a sink. Graph-wide mode
        prefixes each batch with the node's qualified path.

        Nodes with a ``guard`` require ``actor`` such that ``guard(actor, "observe")`` is
        true (default actor is system — :func:`~graphrefly.core.guard.system_actor`).

        When ``structured=True`` (or ``timeline`` / ``causal`` / ``derived``),
        returns an :class:`ObserveResult` that accumulates
        events, tracks counts, and provides a ``dispose()`` method.

        Progressive disclosure via ``detail``:

        - ``detail="full"``: implies ``structured=True, timeline=True, causal=True, derived=True``
        - ``detail="minimal"``: implies ``structured=True``; only DATA events are added to
          ``events[]`` (DIRTY/RESOLVED/COMPLETE/ERROR still update counts/flags)
        - ``detail="standard"``: current default behavior (all message types in events)
        - Individual flags override the detail level.

        Args:
            path: Optional node path (``None`` for graph-wide).
            actor: Optional actor for guard checking.
            structured: If ``True``, return an :class:`ObserveResult` instead of
                a raw :class:`GraphObserveSource`.
            timeline: Include ``timestamp_ns`` and ``in_batch`` on events.
            causal: Include trigger dep info (single-path derived/compute nodes).
            derived: Include per-evaluation dep snapshots (single-path derived/compute nodes).
            detail: Progressive disclosure level: ``"minimal"``, ``"standard"``, or ``"full"``.
        """
        # Apply detail-level defaults (individual flags override)
        _detail_minimal = False
        if detail == "full":
            if not timeline:
                timeline = True
            if not causal:
                causal = True
            if not derived:
                derived = True
        elif detail == "minimal":
            _detail_minimal = True
        # detail="standard" is default behavior, no changes needed

        source = GraphObserveSource(self, path, actor)
        wants_structured = (
            structured or timeline or causal or derived or detail in ("minimal", "full")
        )
        if not wants_structured or not self.inspector_enabled:
            return source
        result = ObserveResult()
        # Store refs for expand()
        result._graph = self
        result._path = path
        result._observe_opts = {
            "actor": actor,
            "structured": structured,
            "timeline": timeline,
            "causal": causal,
            "derived": derived,
            "detail": detail,
        }
        last_trigger_dep_index: int | None = None
        last_run_dep_values: list[Any] | None = None
        detach_hook: Callable[[], None] | None = None

        def _base_event(
            evt_type: str, *, data: Any = None, event_path: str | None = None
        ) -> dict[str, Any]:
            entry: dict[str, Any] = {"type": evt_type}
            if event_path is not None:
                entry["path"] = event_path
            if data is not None:
                entry["data"] = data
            if timeline:
                entry["timestamp_ns"] = monotonic_ns()
                entry["in_batch"] = is_batching()
            return entry

        if (causal or derived) and path is not None:
            n = self.node(path)
            if isinstance(n, NodeImpl):

                def _hook(event: dict[str, Any]) -> None:
                    nonlocal last_trigger_dep_index, last_run_dep_values
                    kind = event.get("kind")
                    if kind == "dep_message":
                        idx = event.get("dep_index")
                        last_trigger_dep_index = int(idx) if isinstance(idx, int) else None
                        return
                    if kind == "run":
                        dep_vals_raw = event.get("dep_values")
                        dep_vals = (
                            list(dep_vals_raw)
                            if isinstance(dep_vals_raw, list)
                            else list(dep_vals_raw or [])
                        )
                        last_run_dep_values = dep_vals
                        if derived:
                            de = _base_event("derived")
                            de["dep_values"] = dep_vals
                            result.events.append(de)

                detach_hook = n._set_inspector_hook(_hook)

        if path is not None:

            def _sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        val = m[1] if len(m) > 1 else None
                        result.values[path] = val
                        event = _base_event("data", data=val)
                        if causal and last_run_dep_values is not None:
                            event["trigger_dep_index"] = last_trigger_dep_index
                            trigger_dep = None
                            if (
                                isinstance(last_trigger_dep_index, int)
                                and last_trigger_dep_index >= 0
                                and isinstance(n, NodeImpl)
                                and last_trigger_dep_index < len(n._deps)
                            ):
                                trigger_dep = n._deps[last_trigger_dep_index]
                                event["trigger_dep_name"] = trigger_dep.name
                            # V0 backfill: include triggering dep's version (§6.0b).
                            tv = trigger_dep.v if trigger_dep is not None else None
                            if tv is not None:
                                event["trigger_version"] = {"id": tv.id, "version": tv.version}
                            event["dep_values"] = list(last_run_dep_values)
                        result.events.append(event)
                    elif t is MessageType.DIRTY:
                        result.dirty_count += 1
                        if not _detail_minimal:
                            result.events.append(_base_event("dirty"))
                    elif t is MessageType.RESOLVED:
                        result.resolved_count += 1
                        if not _detail_minimal:
                            event = _base_event("resolved")
                            if causal and last_run_dep_values is not None:
                                event["trigger_dep_index"] = last_trigger_dep_index
                                trigger_dep = None
                                if (
                                    isinstance(last_trigger_dep_index, int)
                                    and last_trigger_dep_index >= 0
                                    and isinstance(n, NodeImpl)
                                    and last_trigger_dep_index < len(n._deps)
                                ):
                                    trigger_dep = n._deps[last_trigger_dep_index]
                                    event["trigger_dep_name"] = trigger_dep.name
                                # V0 backfill: include triggering dep's version (§6.0b).
                                tv = trigger_dep.v if trigger_dep is not None else None
                                if tv is not None:
                                    event["trigger_version"] = {"id": tv.id, "version": tv.version}
                                event["dep_values"] = list(last_run_dep_values)
                            result.events.append(event)
                    elif t is MessageType.COMPLETE:
                        if not result.errored:
                            result.completed_cleanly = True
                        if not _detail_minimal:
                            result.events.append(_base_event("complete"))
                    elif t is MessageType.ERROR:
                        result.errored = True
                        if not _detail_minimal:
                            result.events.append(
                                _base_event("error", data=m[1] if len(m) > 1 else None)
                            )

            unsub = source.subscribe(_sink)
        else:

            def _graph_sink(qpath: str, msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        val = m[1] if len(m) > 1 else None
                        result.values[qpath] = val
                        result.events.append(_base_event("data", data=val, event_path=qpath))
                    elif t is MessageType.DIRTY:
                        result.dirty_count += 1
                        if not _detail_minimal:
                            result.events.append(_base_event("dirty", event_path=qpath))
                    elif t is MessageType.RESOLVED:
                        result.resolved_count += 1
                        if not _detail_minimal:
                            result.events.append(_base_event("resolved", event_path=qpath))
                    elif t is MessageType.COMPLETE:
                        if not result.errored:
                            result.completed_cleanly = True
                        if not _detail_minimal:
                            result.events.append(_base_event("complete", event_path=qpath))
                    elif t is MessageType.ERROR:
                        result.errored = True
                        if not _detail_minimal:
                            result.events.append(
                                _base_event(
                                    "error",
                                    data=m[1] if len(m) > 1 else None,
                                    event_path=qpath,
                                )
                            )

            unsub = source.subscribe(_graph_sink)

        def _dispose() -> None:
            unsub()
            if detach_hook is not None:
                detach_hook()

        result._dispose_fn = _dispose
        return result

    def spy(
        self,
        path: str | None = None,
        *,
        actor: Any | None = None,
        include_types: list[str] | tuple[str, ...] | None = None,
        exclude_types: list[str] | tuple[str, ...] | None = None,
        theme: str | dict[str, str] | None = "ansi",
        format: str = "pretty",
        logger: Callable[[str, dict[str, Any]], None] | None = None,
        timeline: bool = True,
        causal: bool = False,
        derived: bool = False,
    ) -> SpyHandle:
        """Attach a live debugger that logs protocol events as they arrive.

        Wraps :meth:`observe` and emits each formatted event via ``output``
        (default ``print``). Supports one-node and graph-wide modes, event
        filtering, and ANSI color themes. Returns a :class:`SpyHandle` with the
        accumulated :class:`ObserveResult` and a ``dispose()`` method.

        Args:
            path: Node path to observe (``None`` = entire graph).
            output: Callable receiving each formatted log line (default ``print``).
            theme: ANSI color theme: ``"ansi"`` (default), ``"none"``, or a custom
                ``dict`` of ANSI codes keyed by event type.
            filter: Set of message type labels to include (``None`` = all types).
            actor: Optional actor context for guarded nodes.

        Returns:
            A :class:`SpyHandle` wrapping the live :class:`ObserveResult`.

        Example:
            ```python
            from graphrefly import Graph, state
            g = Graph("g")
            g.add("x", state(0))
            handle = g.spy("x")
            g.set("x", 1)
            handle.dispose()
            ```
        """
        include = set(include_types) if include_types is not None else None
        exclude = set(exclude_types or [])
        colors = _resolve_spy_theme(theme)
        sink = logger or (lambda line, _event: print(line))

        def should_log(event_type: str) -> bool:
            if include is not None and event_type not in include:
                return False
            return event_type not in exclude

        def render_event(event: dict[str, Any]) -> str:
            if format == "json":
                try:
                    return json.dumps(event)
                except Exception:
                    fallback = {
                        "type": event.get("type"),
                        "path": event.get("path"),
                        "data": "[unserializable]",
                    }
                    return json.dumps(fallback)
            event_type = str(event.get("type") or "event")
            color = colors.get(event_type, "")
            path_part = ""
            if "path" in event:
                path_part = f"{colors['path']}{event['path']}{colors['reset']} "
            has_data = "data" in event and event["data"] is not None
            data_part = f" {_describe_data(event['data'])}" if has_data else ""
            trigger = ""
            if event.get("trigger_dep_name") is not None:
                trigger = f" <- {event['trigger_dep_name']}"
            elif event.get("trigger_dep_index") is not None:
                trigger = f" <- #{event['trigger_dep_index']}"
            batch_part = " [batch]" if event.get("in_batch") else ""
            return (
                f"{path_part}{color}{event_type.upper()}{colors['reset']}"
                f"{data_part}{trigger}{batch_part}"
            )

        # --- Helper: build an event dict from a raw message and accumulate into result ---
        def _push_event(result: ObserveResult, event_path: str | None, m: tuple[Any, ...]) -> None:
            event_type = _message_type_label(m[0])
            if event_type is None:
                return
            event: dict[str, Any] = {"type": event_type}
            if event_path is not None:
                event["path"] = event_path
            if timeline:
                event["timestamp_ns"] = monotonic_ns()
                event["in_batch"] = is_batching()
            if event_type in ("data", "error"):
                event["data"] = m[1] if len(m) > 1 else None
            if event_type == "data" and event_path is not None:
                result.values[event_path] = event.get("data")
            elif event_type == "dirty":
                result.dirty_count += 1
            elif event_type == "resolved":
                result.resolved_count += 1
            elif event_type == "complete" and not result.errored:
                result.completed_cleanly = True
            elif event_type == "error":
                result.errored = True
            result.events.append(event)
            if should_log(event_type):
                sink(render_event(event), event)

        # --- Inspector-disabled fallback: manual accumulator via raw observe ---
        if not self.inspector_enabled:
            result = ObserveResult()
            unsub: Callable[[], None]

            if path is not None:
                stream = self.observe(path, actor=actor)
                if not isinstance(stream, GraphObserveSource):
                    msg = "spy expected GraphObserveSource in raw mode"
                    raise TypeError(msg)

                def _on_path_msgs(msgs: Any) -> None:
                    for m in msgs:
                        _push_event(result, path, m)

                unsub = stream.subscribe(_on_path_msgs)
            else:
                stream = self.observe(actor=actor)
                if not isinstance(stream, GraphObserveSource):
                    msg = "spy expected GraphObserveSource in raw mode"
                    raise TypeError(msg)

                def _on_qpath_msgs(qpath: str, msgs: Any) -> None:
                    for m in msgs:
                        _push_event(result, qpath, m)

                unsub = stream.subscribe(_on_qpath_msgs)

            result._dispose_fn = unsub
            return SpyHandle(result=result)

        # --- Inspector-enabled path: use structured observe + flush loop ---
        structured_candidate = self.observe(
            path,
            actor=actor,
            structured=True,
            timeline=timeline,
            causal=causal,
            derived=derived,
        )
        result = (
            structured_candidate
            if isinstance(structured_candidate, ObserveResult)
            else ObserveResult()
        )
        structured_cleanup = (
            structured_candidate._dispose_fn
            if isinstance(structured_candidate, ObserveResult)
            else None
        )

        cursor = 0

        def flush_new_events() -> None:
            nonlocal cursor
            next_events = result.events[cursor:]
            cursor = len(result.events)
            for event in next_events:
                event_type = str(event.get("type") or "")
                if not should_log(event_type):
                    continue
                sink(render_event(event), event)

        if path is not None:

            def on_messages(msgs: Messages) -> None:
                for m in msgs:
                    flush_new_events()
                    if isinstance(structured_candidate, ObserveResult):
                        continue
                    _push_event(result, path, m)

            stream_raw = self.observe(path, actor=actor)
            if not isinstance(stream_raw, GraphObserveSource):
                msg = "spy expected GraphObserveSource in raw mode"
                raise TypeError(msg)
            unsub = stream_raw.subscribe(on_messages)
        else:

            def on_graph_messages(qpath: str, msgs: Messages) -> None:
                for m in msgs:
                    flush_new_events()
                    if isinstance(structured_candidate, ObserveResult):
                        continue
                    _push_event(result, qpath, m)

            stream_raw = self.observe(actor=actor)
            if not isinstance(stream_raw, GraphObserveSource):
                msg = "spy expected GraphObserveSource in raw mode"
                raise TypeError(msg)
            unsub = stream_raw.subscribe(on_graph_messages)

        def _dispose() -> None:
            unsub()
            flush_new_events()
            if structured_cleanup is not None:
                structured_cleanup()

        result._dispose_fn = _dispose
        return SpyHandle(result=result)

    def dump_graph(
        self,
        *,
        actor: Any | None = None,
        filter: Any = None,
        format: str = "pretty",
        indent: int = 2,
        include_edges: bool = True,
        include_subgraphs: bool = True,
        logger: Callable[[str], None] | None = None,
    ) -> str:
        """Return a CLI/debug-friendly text dump of the graph topology.

        Built on :meth:`describe`; formats node names, types, statuses, and edges.

        Args:
            actor: Optional actor context for scoping guarded nodes.

        Returns:
            A multi-line ``str`` summary of the graph.

        Example:
            ```python
            from graphrefly import Graph, state
            g = Graph("g")
            g.add("x", state(1))
            print(g.dump_graph())
            ```
        """
        described = self.describe(actor=actor, filter=filter, detail="standard")
        if format == "json":
            payload = {
                "name": described["name"],
                "nodes": described["nodes"],
                "edges": described["edges"] if include_edges else [],
                "subgraphs": described["subgraphs"] if include_subgraphs else [],
            }
            text = json.dumps(payload, indent=indent, sort_keys=True)
            if logger is not None:
                logger(text)
            return text

        lines: list[str] = [f"Graph {described['name']}", "Nodes:"]
        for node_path in sorted(described["nodes"]):
            entry = described["nodes"][node_path]
            lines.append(
                f"- {node_path} ({entry.get('type')}/{entry.get('status')}): "
                f"{_describe_data(entry.get('value'))}"
            )
        if include_edges:
            lines.append("Edges:")
            for edge in described["edges"]:
                lines.append(f"- {edge['from']} -> {edge['to']}")
        if include_subgraphs:
            lines.append("Subgraphs:")
            for subgraph in described["subgraphs"]:
                lines.append(f"- {subgraph}")
        text = "\n".join(lines)
        if logger is not None:
            logger(text)
        return text

    def node(self, path: str) -> NodeImpl[Any]:
        """Return the node for a local name or a ``::``-qualified path.

        Args:
            path: Local name or fully-qualified path.

        Returns:
            The :class:`~graphrefly.core.node.NodeImpl` at that path.

        Example:
            ```python
            from graphrefly import Graph, state
            g = Graph("g")
            g.add("x", state(0))
            assert g.node("x").get() == 0
            ```
        """
        if PATH_SEP in path:
            return self.resolve(path)
        with self._locked():
            try:
                return self._nodes[path]
            except KeyError as e:
                raise KeyError(path) from e

    def get(self, node_name: str) -> Any:
        """Return the current cached value of the node at ``node_name``.

        Shorthand for ``graph.node(node_name).get()``. Accepts ``::``-qualified paths.

        Args:
            node_name: Local name or qualified path.

        Returns:
            The node's last settled value, or ``None`` if not yet settled.

        Example:
            ```python
            from graphrefly import Graph, state
            g = Graph("g")
            g.add("x", state(7))
            assert g.get("x") == 7
            ```
        """
        return self.node(node_name).get()

    def set(
        self,
        node_name: str,
        value: Any,
        *,
        actor: Any | None = None,
        internal: bool = False,
    ) -> None:
        """Set the value of a node by pushing a ``DATA`` message.

        Shorthand for ``graph.node(node_name).down([(DATA, value)], actor=actor)``.
        Accepts ``::``-qualified paths.

        Args:
            node_name: Local name or qualified path.
            value: New value to push as ``DATA``.
            actor: Optional actor context checked against the node's guard.

        Example:
            ```python
            from graphrefly import Graph, state
            g = Graph("g")
            g.add("x", state(0))
            g.set("x", 42)
            assert g.get("x") == 42
            ```
        """
        try:
            self.node(node_name).down(
                [(MessageType.DATA, value)],
                actor=actor,
                internal=internal,
                guard_action="write",
            )
        except GuardDenied as e:
            raise GuardDenied(e.actor, node_name, e.action) from e

    def connect(self, from_path: str, to_path: str) -> None:
        """Record a pure graph wire from ``from_path`` to ``to_path``.

        ``to`` must already list ``from`` as a constructor dependency. This is an
        idempotent bookkeeping operation; it does not change ``NodeImpl`` dep lists.
        Both paths accept local names or ``mount::...`` qualified paths.

        Args:
            from_path: Path of the upstream (source) node.
            to_path: Path of the downstream (sink) node.

        Example:
            ```python
            from graphrefly import Graph, state, node
            g = Graph("g")
            x = state(1)
            y = node([x], lambda deps, _: deps[0])
            g.add("x", x); g.add("y", y)
            g.connect("x", "y")
            ```
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
        """Remove a registered edge between two nodes.

        **Registry-only (§C resolved):** This drops the edge from the graph's edge
        registry only. It does **not** mutate the target node's constructor-time
        dependency list, bitmasks, or upstream subscriptions. Message flow follows
        constructor-time deps, not the edge registry. For runtime dep rewiring, use
        :func:`~graphrefly.core.dynamic_node.dynamic_node`.

        Raises :exc:`ValueError` if the edge was never registered. Both paths
        accept ``::``-qualified formats.

        Args:
            from_path: Path of the upstream node.
            to_path: Path of the downstream node.

        Example:
            ```python
            from graphrefly import Graph, state, node
            g = Graph("g")
            x = state(0)
            y = node([x], lambda deps, _: deps[0])
            g.add("x", x); g.add("y", y)
            g.connect("x", "y")
            g.disconnect("x", "y")
            ```
        """
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
        """Return all registered ``(from_name, to_name)`` edge pairs (read-only).

        Returns:
            A ``frozenset`` of ``(from_name, to_name)`` string tuples.

        Example:
            ```python
            from graphrefly import Graph, state, node
            g = Graph("g")
            x = state(0); y = node([x], lambda d, _: d[0])
            g.add("x", x); g.add("y", y)
            g.connect("x", "y")
            assert ("x", "y") in g.edges()
            ```
        """
        with self._locked():
            return frozenset(self._edges)

    def to_mermaid(self, *, direction: str = "LR") -> str:
        """Export current topology as Mermaid flowchart text.

        Uses :meth:`describe` to render qualified node paths and registered edges.

        Args:
            direction: ``"TD"``, ``"LR"``, ``"BT"``, or ``"RL"`` (default ``"LR"``).

        Returns:
            Mermaid source string.
        """
        direction = _normalize_diagram_direction(direction)
        described = self.describe()
        paths = sorted(described["nodes"].keys())
        ids = {path: f"n{i}" for i, path in enumerate(paths)}
        lines: list[str] = [f"flowchart {direction}"]
        for path in paths:
            lines.append(f'  {ids[path]}["{_escape_mermaid_label(path)}"]')
        drawn: set[tuple[str, str]] = set()
        for edge in described["edges"]:
            from_id = ids.get(edge["from"])
            to_id = ids.get(edge["to"])
            if from_id is None or to_id is None:
                continue
            pair = (edge["from"], edge["to"])
            if pair not in drawn:
                drawn.add(pair)
                lines.append(f"  {from_id} --> {to_id}")
        for path in paths:
            node_desc = described["nodes"][path]
            for dep in node_desc.get("deps", []):
                pair = (dep, path)
                if pair in drawn:
                    continue
                from_id = ids.get(dep)
                to_id = ids.get(path)
                if from_id is None or to_id is None:
                    continue
                drawn.add(pair)
                lines.append(f"  {from_id} --> {to_id}")
        return "\n".join(lines)

    def to_d2(self, *, direction: str = "LR") -> str:
        """Export current topology as D2 diagram text.

        Uses :meth:`describe` to render qualified node paths and registered edges.

        Args:
            direction: ``"TD"``, ``"LR"``, ``"BT"``, or ``"RL"`` (default ``"LR"``).

        Returns:
            D2 source string.
        """
        direction = _normalize_diagram_direction(direction)
        described = self.describe()
        paths = sorted(described["nodes"].keys())
        ids = {path: f"n{i}" for i, path in enumerate(paths)}
        lines: list[str] = [f"direction: {_d2_direction_from_graph_direction(direction)}"]
        for path in paths:
            lines.append(f'{ids[path]}: "{_escape_d2_label(path)}"')
        drawn: set[tuple[str, str]] = set()
        for edge in described["edges"]:
            from_id = ids.get(edge["from"])
            to_id = ids.get(edge["to"])
            if from_id is None or to_id is None:
                continue
            pair = (edge["from"], edge["to"])
            if pair not in drawn:
                drawn.add(pair)
                lines.append(f"{from_id} -> {to_id}")
        for path in paths:
            node_desc = described["nodes"][path]
            for dep in node_desc.get("deps", []):
                pair = (dep, path)
                if pair in drawn:
                    continue
                from_id = ids.get(dep)
                to_id = ids.get(path)
                if from_id is None or to_id is None:
                    continue
                drawn.add(pair)
                lines.append(f"{from_id} -> {to_id}")
        return "\n".join(lines)

    def annotate(self, path: str, reason: str) -> None:
        """Store an annotation for ``path`` and record it in the trace ring buffer.

        Annotations are informational labels attached to node paths for debugging
        and auditing. Each call also appends a :class:`TraceEntry` to the ring buffer.

        Args:
            path: Qualified node path.
            reason: Human-readable annotation text.
        """
        if not self.inspector_enabled:
            return
        self.resolve(path)
        with self._locked():
            self._annotations[path] = reason
            self._trace_ring.append(
                TraceEntry(timestamp_ns=monotonic_ns(), path=path, reason=reason)
            )

    def trace_log(self) -> list[TraceEntry]:
        """Return a copy of the trace ring buffer (most recent last).

        Returns:
            A list of :class:`TraceEntry` objects.
        """
        if not self.inspector_enabled:
            return []
        with self._locked():
            return list(self._trace_ring)

    @staticmethod
    def diff(a: dict[str, Any], b: dict[str, Any]) -> GraphDiffResult:
        """Structural diff of two :meth:`describe` outputs.

        Compares ``nodes``, ``edges``, and ``subgraphs`` between snapshots *a* and *b*.

        Args:
            a: First describe output (the "before" state).
            b: Second describe output (the "after" state).

        Returns:
            A :class:`GraphDiffResult` with added, removed, and changed items.
        """
        a_nodes = set(a.get("nodes", {}).keys())
        b_nodes = set(b.get("nodes", {}).keys())
        added_nodes = sorted(b_nodes - a_nodes)
        removed_nodes = sorted(a_nodes - b_nodes)
        changed_nodes: list[GraphDiffNodeChange] = []
        for p in sorted(a_nodes & b_nodes):
            na = a["nodes"][p]
            nb = b["nodes"][p]
            if not isinstance(na, dict) or not isinstance(nb, dict):
                if na != nb:
                    changed_nodes.append(
                        GraphDiffNodeChange(path=p, field="node", from_value=na, to_value=nb)
                    )
                continue
            # V0 optimization: skip value comparison when both nodes have matching versions.
            av = na.get("v")
            bv = nb.get("v")
            if (
                av is not None
                and bv is not None
                and av.get("id") == bv.get("id")
                and av.get("version") == bv.get("version")
            ):
                for key in ("type", "status"):
                    va = na.get(key)
                    vb = nb.get(key)
                    if va != vb:
                        changed_nodes.append(
                            GraphDiffNodeChange(path=p, field=key, from_value=va, to_value=vb)
                        )
                continue
            for key in ("type", "status", "value"):
                va = na.get(key)
                vb = nb.get(key)
                if va != vb:
                    changed_nodes.append(
                        GraphDiffNodeChange(
                            path=p,
                            field=key,
                            from_value=va,
                            to_value=vb,
                        )
                    )

        a_edges = {(e["from"], e["to"]) for e in a.get("edges", [])}
        b_edges = {(e["from"], e["to"]) for e in b.get("edges", [])}
        added_edges = [GraphDiffEdge(from_node=f, to_node=t) for f, t in sorted(b_edges - a_edges)]
        removed_edges = [
            GraphDiffEdge(from_node=f, to_node=t) for f, t in sorted(a_edges - b_edges)
        ]

        a_subs = set(a.get("subgraphs", []))
        b_subs = set(b.get("subgraphs", []))
        added_subgraphs = sorted(b_subs - a_subs)
        removed_subgraphs = sorted(a_subs - b_subs)

        return GraphDiffResult(
            nodesAdded=added_nodes,
            nodesRemoved=removed_nodes,
            nodesChanged=changed_nodes,
            edgesAdded=added_edges,
            edgesRemoved=removed_edges,
            subgraphsAdded=added_subgraphs,
            subgraphsRemoved=removed_subgraphs,
        )


def reachable(
    described: dict[str, Any],
    from_path: str,
    direction: str,
    *,
    max_depth: int | None = None,
) -> list[str]:
    """Perform a reachability query over a :meth:`Graph.describe` snapshot.

    Traversal combines dependency links (``deps``) and explicit graph edges
    (``edges``):

    - ``"upstream"``: follows ``deps`` and incoming edges.
    - ``"downstream"``: follows reverse-``deps`` and outgoing edges.

    Args:
        described: Output of ``graph.describe()``.
        from_path: Start path (qualified node path).
        direction: Either ``"upstream"`` or ``"downstream"``.
        max_depth: Optional hop limit; ``0`` returns an empty list.

    Returns:
        Sorted list of reachable paths, excluding ``from_path``.

    Example:
        ```python
        from graphrefly import Graph, state, node, reachable
        g = Graph("g")
        x = state(0)
        y = node([x], lambda deps, _: deps[0])
        g.add("x", x); g.add("y", y)
        g.connect("x", "y")
        desc = g.describe()
        assert "x" in reachable(desc, "y", "upstream")
        ```
    """
    if not from_path:
        return []
    if direction not in {"upstream", "downstream"}:
        msg = f"reachable direction must be 'upstream' or 'downstream', got {direction!r}"
        raise ValueError(msg)
    if max_depth is not None:
        if type(max_depth) is not int or max_depth < 0:
            msg = f"reachable max_depth must be an int >= 0, got {max_depth!r}"
            raise ValueError(msg)
        if max_depth == 0:
            return []

    nodes_raw = described.get("nodes", {})
    edges_raw = described.get("edges", [])
    if not isinstance(nodes_raw, dict):
        return []
    if not isinstance(edges_raw, list):
        edges_raw = []

    deps_by_path: dict[str, list[str]] = {}
    reverse_deps: dict[str, set[str]] = {}
    incoming_edges: dict[str, set[str]] = {}
    outgoing_edges: dict[str, set[str]] = {}
    universe: set[str] = set()

    for path, node_desc in nodes_raw.items():
        if not isinstance(path, str):
            continue
        universe.add(path)
        deps: list[str] = []
        if isinstance(node_desc, dict):
            raw_deps = node_desc.get("deps")
            if isinstance(raw_deps, list):
                deps = [d for d in raw_deps if isinstance(d, str) and d]
        deps_by_path[path] = deps
        for dep in deps:
            universe.add(dep)
            reverse_deps.setdefault(dep, set()).add(path)

    for edge in edges_raw:
        if not isinstance(edge, dict):
            continue
        edge_from = edge.get("from")
        edge_to = edge.get("to")
        if not isinstance(edge_from, str) or not edge_from:
            continue
        if not isinstance(edge_to, str) or not edge_to:
            continue
        universe.add(edge_from)
        universe.add(edge_to)
        outgoing_edges.setdefault(edge_from, set()).add(edge_to)
        incoming_edges.setdefault(edge_to, set()).add(edge_from)

    if from_path not in universe:
        return []

    def neighbors(path: str) -> list[str]:
        if direction == "upstream":
            return deps_by_path.get(path, []) + sorted(incoming_edges.get(path, set()))
        return sorted(reverse_deps.get(path, set())) + sorted(outgoing_edges.get(path, set()))

    visited: set[str] = {from_path}
    out: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(from_path, 0)])
    while queue:
        cur, depth = queue.popleft()
        if max_depth is not None and depth >= max_depth:
            continue
        for nb in neighbors(cur):
            if not nb or nb in visited:
                continue
            visited.add(nb)
            out.add(nb)
            queue.append((nb, depth + 1))
    return sorted(out)


def _finish_resolve_from_node(base: NodeImpl[Any], tail: list[str], path: str) -> NodeImpl[Any]:
    if not tail:
        return base
    if tail[0] == GRAPH_META_SEGMENT:
        return _resolve_meta_chain(base, tail, path)
    msg = f"{path!r}: cannot traverse past a registered node (not a subgraph)"
    raise KeyError(msg)


def _resolve_meta_chain(n: NodeImpl[Any], parts: list[str], path: str) -> NodeImpl[Any]:
    if not parts:
        return n
    if parts[0] != GRAPH_META_SEGMENT:
        msg = f"expected {GRAPH_META_SEGMENT!r} segment in meta path {path!r}"
        raise KeyError(msg)
    if len(parts) < 2:
        msg = f"meta path requires a key after {GRAPH_META_SEGMENT!r} in {path!r}"
        raise ValueError(msg)
    key = parts[1]
    try:
        child = n.meta[key]
    except KeyError as e:
        raise KeyError(f"unknown meta key {key!r} in path {path!r}") from e
    rest = parts[2:]
    return _resolve_meta_chain(child, rest, path) if rest else child


_META_FILTERED_TYPES = frozenset(
    {MessageType.TEARDOWN, MessageType.INVALIDATE, MessageType.COMPLETE, MessageType.ERROR}
)


def _filter_meta_messages(messages: Messages) -> Messages:
    """Strip lifecycle-destructive messages before delivering to meta companions (spec §2.3)."""
    return [m for m in messages if m[0] not in _META_FILTERED_TYPES]


def _signal_node_subtree(
    n: NodeImpl[Any],
    messages: Messages,
    visited: set[int],
    actor: dict[str, Any],
    path: str,
    *,
    internal: bool = False,
) -> None:
    nid = id(n)
    if nid in visited:
        return
    visited.add(nid)
    if internal:
        n.down(messages, internal=True)
    else:
        try:
            n.down(messages, actor=actor, guard_action="signal")
        except GuardDenied as e:
            raise GuardDenied(e.actor, path, e.action) from e
    meta_msgs = _filter_meta_messages(messages)
    if not meta_msgs:
        return
    for k in sorted(n.meta):
        mp = f"{path}{PATH_SEP}{GRAPH_META_SEGMENT}{PATH_SEP}{k}"
        _signal_node_subtree(n.meta[k], meta_msgs, visited, actor, mp, internal=internal)


def _collect_observe_targets(g: Graph, prefix: str) -> list[tuple[str, NodeImpl[Any]]]:
    """Collect all observe targets and sort by full qualified path (code-point order).

    Cross-language alignment: both Python and TypeScript use full-path code-point
    sort so that ``observe()`` subscription order is deterministic and identical
    across implementations.
    """
    out: list[tuple[str, NodeImpl[Any]]] = []
    _collect_observe_targets_unsorted(g, prefix, out)
    out.sort(key=lambda pair: pair[0])
    return out


def _collect_observe_targets_unsorted(
    g: Graph, prefix: str, out: list[tuple[str, NodeImpl[Any]]]
) -> None:
    """Recursively collect all targets without sorting (helper for full-path sort)."""
    with g._locked():
        mounts = list(g._mounts.items())
        node_items = list(g._nodes.items())
    for mn, ch in mounts:
        wp = f"{prefix}{mn}{PATH_SEP}" if prefix else f"{mn}{PATH_SEP}"
        _collect_observe_targets_unsorted(ch, wp, out)
    for name, n in node_items:
        bp = f"{prefix}{name}" if prefix else name
        out.append((bp, n))
        _append_meta_observe_targets(n, bp, out)


def _append_meta_observe_targets(
    n: NodeImpl[Any], base_path: str, out: list[tuple[str, NodeImpl[Any]]]
) -> None:
    for k in sorted(n.meta):
        m = n.meta[k]
        mp = f"{base_path}{PATH_SEP}{GRAPH_META_SEGMENT}{PATH_SEP}{k}"
        out.append((mp, m))
        _append_meta_observe_targets(m, mp, out)


def _node_describe_for_graph(
    n: NodeImpl[Any], paths_by_id: dict[int, str], include_fields: set[str] | None = None
) -> dict[str, Any]:
    d = describe_node(n, include_fields)
    d["deps"] = [paths_by_id.get(id(dep), dep.name or "") for dep in n._deps]
    d.pop("name", None)
    return d


def _collect_edges_qualified(g: Graph, prefix: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with g._locked():
        loc_edges = frozenset(g._edges)
        mounts = sorted(g._mounts.items())
    for f, t in loc_edges:
        if PATH_SEP in f or PATH_SEP in t:
            rows.append({"from": f, "to": t})
        else:
            fq = f"{prefix}{f}" if prefix else f
            tq = f"{prefix}{t}" if prefix else t
            rows.append({"from": fq, "to": tq})
    for mn, ch in mounts:
        wp = f"{prefix}{mn}{PATH_SEP}" if prefix else f"{mn}{PATH_SEP}"
        rows.extend(_collect_edges_qualified(ch, wp))
    return rows


def _collect_subgraphs_qualified(g: Graph, prefix: str) -> list[str]:
    out: list[str] = []
    with g._locked():
        mounts = list(g._mounts.items())
    for mn, ch in mounts:
        q = f"{prefix}{mn}" if prefix else mn
        out.append(q)
        out.extend(_collect_subgraphs_qualified(ch, f"{q}{PATH_SEP}"))
    return out


class GraphObserveSource:
    """Live observation handle from :meth:`Graph.observe` (GRAPHREFLY-SPEC §3.6)."""

    __slots__ = ("_actor", "_graph", "_path")

    def __init__(self, graph: Graph, path: str | None, actor: Any | None) -> None:
        self._graph = graph
        self._path = path
        self._actor = actor

    def subscribe(self, sink: Any) -> Any:
        """Attach ``sink``.

        With a single-node path, ``sink(messages)`` receives :class:`Messages`.

        With the graph-wide stream (``path is None``), ``sink(qualified_path, messages)``.
        Returns an unsubscribe callable.
        """
        act = self._actor
        if self._path is not None:
            n = self._graph.node(self._path)
            try:
                return n.subscribe(sink, actor=act)
            except GuardDenied as e:
                raise GuardDenied(e.actor, self._path, e.action) from e
        unsubs: list[Any] = []
        a = normalize_actor(act)
        for qpath, n in _collect_observe_targets(self._graph, ""):
            if not _node_allows_observe(n, a):
                continue

            def on_msgs(msgs: Messages, *, _p: str = qpath) -> None:
                sink(_p, msgs)

            unsubs.append(n.subscribe(on_msgs, actor=act))

        def cleanup() -> None:
            for u in unsubs:
                u()

        return cleanup

    def up(self, messages: Messages, path: str | None = None) -> None:
        """Send messages upstream toward the observed node's sources.

        For single-node observation (``self._path is not None``), *path* is
        ignored and messages go to the observed node.  For graph-wide
        observation, *path* must be provided to target a specific node.

        If the target node's guard denies the action, the messages are
        silently dropped (aligned with TS).  This prevents ``GuardDenied``
        from propagating into backpressure controller callbacks.
        """
        target_path = path if self._path is None else self._path
        if target_path is None:
            msg = "up() on graph-wide observe requires a path argument"
            raise ValueError(msg)
        n = self._graph.node(target_path)
        up_fn = getattr(n, "up", None)
        if up_fn is not None:
            try:
                up_fn(messages)
            except GuardDenied:
                return


def _teardown_mounted_graph(root: Graph) -> None:
    with root._locked():
        mounts = list(root._mounts.values())
    for m in mounts:
        _teardown_mounted_graph(m)
    with root._locked():
        nodes = list(root._nodes.values())
    for n in nodes:
        n.down([(MessageType.TEARDOWN,)], internal=True)


def _signal_graph(
    g: Graph,
    messages: Messages,
    visited: set[int],
    actor: dict[str, Any],
    prefix: str,
    *,
    internal: bool = False,
) -> None:
    with g._locked():
        mounts = sorted(g._mounts.items())
        nodes = sorted(g._nodes.items())
    for mn, m in mounts:
        wp = f"{prefix}{mn}{PATH_SEP}" if prefix else f"{mn}{PATH_SEP}"
        _signal_graph(m, messages, visited, actor, wp, internal=internal)
    for name, n in nodes:
        q = f"{prefix}{name}" if prefix else name
        _signal_node_subtree(n, messages, visited, actor, q, internal=internal)


__all__ = [
    "GRAPH_META_SEGMENT",
    "GRAPH_SNAPSHOT_VERSION",
    "GraphAutoCheckpointHandle",
    "Graph",
    "GraphDiffEdge",
    "GraphDiffNodeChange",
    "GraphDiffResult",
    "GraphObserveSource",
    "META_PATH_SEG",
    "ObserveResult",
    "PATH_SEP",
    "SpyHandle",
    "TraceEntry",
    "reachable",
]
