"""GraphReFly ``node`` primitive — aligned with graphrefly-ts ``src/core/node.ts``."""

from __future__ import annotations

import operator
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from functools import partial
from typing import Any, cast

from graphrefly.core.protocol import Messages, MessageType, emit_with_batch
from graphrefly.core.subgraph_locks import (
    acquire_subgraph_write_lock_with_defer,
    ensure_registered,
    union_nodes,
)

# --- Status & typing (graphrefly-ts node.ts) ---------------------------------

NodeStatus = str  # structural: same strings as TS NodeStatus

# --- BitSet: Python int bitmask (unlimited precision; TS uses int + Uint32Array) ---


class _BitSet:
    __slots__ = ("_bits", "_width")

    def __init__(self, width: int) -> None:
        self._width = width
        self._bits = 0

    def set(self, index: int) -> None:
        self._bits |= 1 << index

    def clear(self, index: int) -> None:
        self._bits &= ~(1 << index)

    def has(self, index: int) -> bool:
        return bool(self._bits & (1 << index))

    def covers(self, other: _BitSet) -> bool:
        ob = other._bits
        return (self._bits & ob) == ob

    def any(self) -> bool:
        return self._bits != 0

    def reset(self) -> None:
        self._bits = 0


def _create_bit_set(size: int) -> _BitSet:
    return _BitSet(size)


def _status_after_message(status: NodeStatus, msg: Message) -> NodeStatus:
    t = msg[0]
    if t == MessageType.DIRTY:
        return "dirty"
    if t == MessageType.DATA:
        return "settled"
    if t == MessageType.RESOLVED:
        return "resolved"
    if t == MessageType.COMPLETE:
        return "completed"
    if t == MessageType.ERROR:
        return "errored"
    if t == MessageType.INVALIDATE:
        return "dirty"
    return status


# Open wire set: first element may be MessageType or any hashable tag (forward compat).
type Message = tuple[Any, Any] | tuple[Any]


def _is_cleanup_fn(value: object) -> bool:
    """Matches TS ``typeof out === 'function'`` (cleanup vs emitted value)."""
    return callable(value)


def _is_node_sequence(value: object) -> bool:
    if not isinstance(value, (list, tuple)):
        return False
    if len(value) == 0:
        return True
    return callable(getattr(value[0], "subscribe", None))


def _is_node_options(value: object) -> bool:
    return isinstance(value, dict) or (
        value is not None
        and not callable(value)
        and not isinstance(value, (list, tuple))
        and hasattr(value, "keys")
    )


def _as_options_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, Mapping):
        return dict(value)
    o: Any = value
    return {k: getattr(o, k) for k in o}


class NodeActions:
    """Imperative ``actions`` object passed to the node compute function."""

    __slots__ = ("_down", "_emit", "_up")

    def __init__(
        self,
        down: Callable[[Messages], None],
        emit: Callable[[Any], None],
        up: Callable[[Messages], None],
    ) -> None:
        self._down = down
        self._emit = emit
        self._up = up

    def down(self, messages: Messages) -> None:
        self._down(messages)

    def emit(self, value: Any) -> None:
        self._emit(value)

    def up(self, messages: Messages) -> None:
        self._up(messages)


NodeFn = Callable[[list[Any], NodeActions], Any]


class SubscribeHints:
    __slots__ = ("single_dep",)

    def __init__(self, *, single_dep: bool = False) -> None:
        self.single_dep = single_dep


class NodeImpl[T]:
    """Internal implementation — use :func:`node` factory."""

    __slots__ = (
        "__weakref__",
        "_actions",
        "_all_deps_complete_mask",
        "_auto_complete",
        "_cache_lock",
        "_cached",
        "_cleanup",
        "_connected",
        "_connecting",
        "_dep_complete_mask",
        "_dep_dirty_mask",
        "_dep_settled_mask",
        "_deps",
        "_down",
        "_emit_auto_value",
        "_equals",
        "_fn",
        "_handle_dep_messages",
        "_has_deps",
        "_last_dep_values",
        "_manual_emit_used",
        "_meta",
        "_name",
        "_opts",
        "_producer_started",
        "_resubscribable",
        "_reset_on_teardown",
        "_run_fn",
        "_sink_count",
        "_single_dep_sink_count",
        "_single_dep_sinks",
        "_sinks",
        "_status",
        "_terminal",
        "_upstream_unsubs",
    )

    def __init__(
        self,
        deps: list[NodeImpl[Any]],
        fn: NodeFn | None,
        opts: dict[str, Any],
    ) -> None:
        self._opts = opts
        self._name: str | None = opts.get("name")
        self._equals: Callable[[Any, Any], bool] = opts.get("equals", operator.is_)
        self._resubscribable: bool = bool(opts.get("resubscribable", False))
        self._reset_on_teardown: bool = bool(opts.get("reset_on_teardown", False))
        self._auto_complete: bool = bool(opts.get("complete_when_deps_complete", True))

        self._fn = fn
        self._deps = deps
        self._has_deps = len(deps) > 0

        self._cache_lock = threading.Lock()
        with self._cache_lock:
            self._cached: T | None = opts.get("initial")
        self._status: NodeStatus = "disconnected" if self._has_deps else "settled"
        self._terminal = False
        self._connected = False
        self._connecting = False
        self._producer_started = False

        self._dep_dirty_mask = _create_bit_set(len(deps))
        self._dep_settled_mask = _create_bit_set(len(deps))
        self._dep_complete_mask = _create_bit_set(len(deps))
        self._all_deps_complete_mask = _create_bit_set(len(deps))
        for i in range(len(deps)):
            self._all_deps_complete_mask.set(i)

        self._last_dep_values: list[Any] | None = None
        self._cleanup: Callable[[], None] | None = None
        self._manual_emit_used = False

        self._sinks: Callable[[Messages], None] | set[Callable[[Messages], None]] | None = None
        self._sink_count = 0
        self._single_dep_sink_count = 0
        self._single_dep_sinks: set[Callable[[Messages], None]] = set()
        self._upstream_unsubs: list[Callable[[], None]] = []

        self._meta: dict[str, NodeImpl[Any]] = {}
        for k, v in (opts.get("meta") or {}).items():
            meta_name = f"{self._name or 'node'}:meta:{k}"
            self._meta[k] = node(initial=v, name=meta_name)

        ensure_registered(self)
        for d in self._deps:
            union_nodes(self, d)
        for meta_node in self._meta.values():
            union_nodes(self, meta_node)

        # --- wire methods ---------------------------------------------------

        def emit_to_sinks(msgs: Messages) -> None:
            if self._sinks is None:
                return
            if isinstance(self._sinks, set):
                for s in self._sinks:
                    s(msgs)
            else:
                self._sinks(msgs)

        def handle_local_lifecycle(messages: Messages) -> None:
            for m in messages:
                t = m[0]
                if t == MessageType.DATA:
                    with self._cache_lock:
                        self._cached = m[1]  # type: ignore[misc]
                self._status = _status_after_message(self._status, m)
                if t in (MessageType.COMPLETE, MessageType.ERROR):
                    self._terminal = True
                if t == MessageType.TEARDOWN:
                    if self._reset_on_teardown:
                        with self._cache_lock:
                            self._cached = None
                    try:
                        for meta_node in self._meta.values():
                            with suppress(Exception):
                                meta_node.down([(MessageType.TEARDOWN,)])
                    finally:
                        self._disconnect_upstream()
                        self._stop_producer()

        def can_skip_dirty() -> bool:
            return self._sink_count == 1 and self._single_dep_sink_count == 1

        def down(messages: Messages) -> None:
            if not messages:
                return
            with acquire_subgraph_write_lock_with_defer(self):
                lifecycle_messages = messages
                sink_messages = messages
                if self._terminal and not self._resubscribable:
                    teardown_only = [m for m in messages if m[0] == MessageType.TEARDOWN]
                    if not teardown_only:
                        return
                    lifecycle_messages = teardown_only
                    sink_messages = teardown_only
                handle_local_lifecycle(lifecycle_messages)
                if can_skip_dirty() and any(
                    m[0] in (MessageType.DATA, MessageType.RESOLVED) for m in sink_messages
                ):
                    filtered = [m for m in sink_messages if m[0] != MessageType.DIRTY]
                    if filtered:
                        emit_with_batch(
                            emit_to_sinks,
                            filtered,
                            strategy="partition",
                            defer_when="depth",
                            subgraph_lock=self,
                        )
                else:
                    emit_with_batch(
                        emit_to_sinks,
                        sink_messages,
                        strategy="partition",
                        defer_when="depth",
                        subgraph_lock=self,
                    )

        def emit_auto_value(value: Any) -> None:
            was_dirty = self._status == "dirty"
            with self._cache_lock:
                cached_snapshot = self._cached
            unchanged = self._equals(cached_snapshot, value)
            if unchanged:
                if was_dirty:
                    down([(MessageType.RESOLVED,)])
                else:
                    down([(MessageType.DIRTY,), (MessageType.RESOLVED,)])
                return
            with self._cache_lock:
                self._cached = cast("T", value)
            if was_dirty:
                down([(MessageType.DATA, value)])
            else:
                down([(MessageType.DIRTY,), (MessageType.DATA, value)])

        def up_actions(messages: Messages) -> None:
            if not self._has_deps:
                return
            for dep in self._deps:
                u = getattr(dep, "up", None)
                if u is not None:
                    u(messages)

        self._down = down
        self._emit_auto_value = emit_auto_value

        self._actions = NodeActions(
            down=lambda msgs: self._manual_down(msgs),
            emit=lambda v: self._manual_emit(v),
            up=up_actions,
        )

        self._run_fn = self._make_run_fn()
        self._handle_dep_messages = self._make_handle_dep_messages()

    def _manual_down(self, messages: Messages) -> None:
        self._manual_emit_used = True
        self._down(messages)

    def _manual_emit(self, value: Any) -> None:
        self._manual_emit_used = True
        self._emit_auto_value(value)

    def _make_run_fn(self) -> Callable[[], None]:
        def run_fn() -> None:
            if self._fn is None:
                return
            # Suppress re-entrant recompute while wiring upstream deps (TS connect order).
            if self._connecting:
                return

            with acquire_subgraph_write_lock_with_defer(self):
                if self._terminal and not self._resubscribable:
                    return
                if self._cleanup is not None:
                    self._cleanup()
                    self._cleanup = None
                self._manual_emit_used = False

                try:
                    dep_values = [d.get() for d in self._deps]
                    if (
                        len(dep_values) > 0
                        and self._last_dep_values is not None
                        and len(self._last_dep_values) == len(dep_values)
                        and all(
                            dep_values[i] is self._last_dep_values[i]
                            for i in range(len(dep_values))
                        )
                    ):
                        if self._status == "dirty":
                            self._down([(MessageType.RESOLVED,)])
                        return
                    self._last_dep_values = list(dep_values)
                    out = self._fn(dep_values, self._actions)
                    if _is_cleanup_fn(out):
                        self._cleanup = out
                        return
                    if self._manual_emit_used:
                        return
                    if out is None:
                        return
                    self._emit_auto_value(out)
                except Exception as err:
                    self._down([(MessageType.ERROR, err)])

        return run_fn

    def _on_dep_dirty(self, index: int) -> None:
        was_dirty = self._dep_dirty_mask.has(index)
        self._dep_dirty_mask.set(index)
        self._dep_settled_mask.clear(index)
        if not was_dirty:
            self._down([(MessageType.DIRTY,)])

    def _on_dep_settled(self, index: int) -> None:
        if not self._dep_dirty_mask.has(index):
            self._on_dep_dirty(index)
        self._dep_settled_mask.set(index)
        if self._dep_dirty_mask.any() and self._dep_settled_mask.covers(self._dep_dirty_mask):
            self._dep_dirty_mask.reset()
            self._dep_settled_mask.reset()
            self._run_fn()

    def _maybe_complete_from_deps(self) -> None:
        if (
            self._auto_complete
            and len(self._deps) > 0
            and self._dep_complete_mask.covers(self._all_deps_complete_mask)
        ):
            self._down([(MessageType.COMPLETE,)])

    def _make_handle_dep_messages(self) -> Callable[[int, Messages], None]:
        def forward_unknown(msg: Message) -> None:
            self._down([msg])

        def handle_dep_messages(index: int, messages: Messages) -> None:
            for msg in messages:
                t = msg[0]
                if self._fn is None:
                    if t == MessageType.COMPLETE and len(self._deps) > 1:
                        self._dep_complete_mask.set(index)
                        self._maybe_complete_from_deps()
                        continue
                    self._down([msg])
                    continue
                if t == MessageType.DIRTY:
                    self._on_dep_dirty(index)
                    continue
                if t in (MessageType.DATA, MessageType.RESOLVED):
                    self._on_dep_settled(index)
                    continue
                if t == MessageType.COMPLETE:
                    self._dep_complete_mask.set(index)
                    self._dep_dirty_mask.clear(index)
                    self._dep_settled_mask.clear(index)
                    if self._dep_dirty_mask.any() and self._dep_settled_mask.covers(
                        self._dep_dirty_mask
                    ):
                        self._dep_dirty_mask.reset()
                        self._dep_settled_mask.reset()
                        self._run_fn()
                    self._maybe_complete_from_deps()
                    continue
                if t == MessageType.ERROR:
                    self._down([msg])
                    continue
                if t in (
                    MessageType.INVALIDATE,
                    MessageType.TEARDOWN,
                    MessageType.PAUSE,
                    MessageType.RESUME,
                ):
                    self._down([msg])
                    continue
                forward_unknown(msg)

        return handle_dep_messages

    def _connect_upstream(self) -> None:
        if not self._has_deps or self._connected:
            return
        self._connected = True
        self._dep_dirty_mask.reset()
        self._dep_settled_mask.reset()
        self._dep_complete_mask.reset()
        self._status = "settled"
        is_single = len(self._deps) == 1 and self._fn is not None
        hints = SubscribeHints(single_dep=True) if is_single else SubscribeHints()
        self._connecting = True
        try:
            for i, dep in enumerate(self._deps):
                unsub = dep.subscribe(partial(self._handle_dep_messages, i), hints)
                self._upstream_unsubs.append(unsub)
        finally:
            self._connecting = False
        if self._fn is not None:
            self._run_fn()

    def _stop_producer(self) -> None:
        if not self._producer_started:
            return
        self._producer_started = False
        if self._cleanup is not None:
            self._cleanup()
            self._cleanup = None

    def _start_producer(self) -> None:
        if len(self._deps) != 0 or self._fn is None or self._producer_started:
            return
        self._producer_started = True
        self._run_fn()

    def _disconnect_upstream(self) -> None:
        if not self._connected:
            return
        for u in self._upstream_unsubs:
            u()
        self._upstream_unsubs.clear()
        self._connected = False
        self._dep_dirty_mask.reset()
        self._dep_settled_mask.reset()
        self._dep_complete_mask.reset()
        self._status = "disconnected"

    def subscribe(
        self,
        sink: Callable[[Messages], None],
        hints: SubscribeHints | None = None,
    ) -> Callable[[], None]:
        with acquire_subgraph_write_lock_with_defer(self):
            if self._terminal and self._resubscribable:
                self._terminal = False
                self._status = "disconnected" if self._has_deps else "settled"

            h = hints or SubscribeHints()
            self._sink_count += 1
            if h.single_dep:
                self._single_dep_sink_count += 1
                self._single_dep_sinks.add(sink)

            if self._sinks is None:
                self._sinks = sink
            elif isinstance(self._sinks, set):
                self._sinks.add(sink)
            else:
                self._sinks = {self._sinks, sink}

            if self._has_deps:
                self._connect_upstream()
            elif self._fn is not None:
                self._start_producer()

        removed = False

        def unsubscribe() -> None:
            nonlocal removed
            with acquire_subgraph_write_lock_with_defer(self):
                if removed:
                    return
                removed = True
                self._sink_count -= 1
                if sink in self._single_dep_sinks:
                    self._single_dep_sink_count -= 1
                    self._single_dep_sinks.discard(sink)

                if self._sinks is None:
                    return
                if isinstance(self._sinks, set):
                    self._sinks.discard(sink)
                    if len(self._sinks) == 1:
                        self._sinks = next(iter(self._sinks))
                    elif len(self._sinks) == 0:
                        self._sinks = None
                elif self._sinks is sink:
                    self._sinks = None

                if self._sinks is None:
                    self._disconnect_upstream()
                    self._stop_producer()

        return unsubscribe

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def status(self) -> NodeStatus:
        return self._status

    @property
    def meta(self) -> dict[str, NodeImpl[Any]]:
        return self._meta

    def get(self) -> T | None:
        with self._cache_lock:
            return self._cached

    def down(self, messages: Messages) -> None:
        self._down(messages)

    def up(self, messages: Messages) -> None:
        """Send messages upstream (no-op on source nodes; matches TS optional ``up``)."""
        if not self._has_deps:
            return
        for dep in self._deps:
            u = getattr(dep, "up", None)
            if u is not None:
                u(messages)

    def unsubscribe(self) -> None:
        """Disconnect from upstream deps (no-op on source nodes)."""
        if not self._has_deps:
            return
        with acquire_subgraph_write_lock_with_defer(self):
            self._disconnect_upstream()


def node(
    deps_or_fn: Sequence[NodeImpl[Any]] | NodeFn | dict[str, Any] | None = None,
    fn_or_opts: NodeFn | dict[str, Any] | None = None,
    opts_arg: dict[str, Any] | None = None,
    **kwargs: Any,
) -> NodeImpl[Any]:
    """Create a reactive node (graphrefly-ts ``node`` overloads)."""
    opts: dict[str, Any] = {**kwargs}
    deps: list[NodeImpl[Any]] = []
    fn: NodeFn | None = None

    if _is_node_sequence(deps_or_fn):
        deps = list(cast("Sequence[NodeImpl[Any]]", deps_or_fn))
        if callable(fn_or_opts):
            fn = fn_or_opts  # narrowed: NodeFn
        if _is_node_options(fn_or_opts):
            opts = {**_as_options_dict(fn_or_opts), **opts}
        elif _is_node_options(opts_arg):
            opts = {**_as_options_dict(opts_arg), **opts}
    elif _is_node_options(deps_or_fn):
        opts = {**_as_options_dict(deps_or_fn), **opts}
    elif callable(deps_or_fn):
        fn = deps_or_fn  # narrowed: NodeFn
        if _is_node_options(fn_or_opts):
            opts = {**_as_options_dict(fn_or_opts), **opts}
    elif deps_or_fn is None:
        if fn_or_opts is not None or opts_arg is not None:
            raise TypeError("node() invalid arguments")
    else:
        raise TypeError(f"node() unexpected first argument: {type(deps_or_fn).__name__}")

    # snake_case option aliases matching TS camelCase in docs
    if "resetOnTeardown" in opts and "reset_on_teardown" not in opts:
        opts["reset_on_teardown"] = opts.pop("resetOnTeardown")
    if "completeWhenDepsComplete" in opts and "complete_when_deps_complete" not in opts:
        opts["complete_when_deps_complete"] = opts.pop("completeWhenDepsComplete")

    return NodeImpl(deps, fn, opts)


# Public alias for type hints
Node = NodeImpl

__all__ = ["Node", "NodeActions", "NodeFn", "NodeImpl", "NodeStatus", "SubscribeHints", "node"]
