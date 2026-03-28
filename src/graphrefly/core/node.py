"""GraphReFly ``node`` primitive — aligned with graphrefly-ts ``src/core/node.ts``."""

from __future__ import annotations

import operator
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from functools import partial
from types import MappingProxyType
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
    if t is MessageType.DIRTY:
        return "dirty"
    if t is MessageType.DATA:
        return "settled"
    if t is MessageType.RESOLVED:
        return "resolved"
    if t is MessageType.COMPLETE:
        return "completed"
    if t is MessageType.ERROR:
        return "errored"
    if t is MessageType.INVALIDATE:
        return "dirty"
    if t is MessageType.TEARDOWN:
        return "disconnected"
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
        "_equals",
        "_fn",
        "_has_deps",
        "_last_dep_values",
        "_manual_emit_used",
        "_meta",
        "_name",
        "_opts",
        "_producer_started",
        "_resubscribable",
        "_reset_on_teardown",
        "_sink_count",
        "_single_dep_sink_count",
        "_single_dep_sinks",
        "_sinks",
        "_status",
        "_terminal",
        "_thread_safe",
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
        self._thread_safe: bool = bool(opts.get("thread_safe", True))

        self._fn = fn
        self._deps = deps
        self._has_deps = len(deps) > 0

        self._cache_lock = threading.Lock() if self._thread_safe else None
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
            self._meta[k] = node(initial=v, name=meta_name, thread_safe=self._thread_safe)

        if self._thread_safe:
            ensure_registered(self)
            for d in self._deps:
                union_nodes(self, d)
            for meta_node in self._meta.values():
                union_nodes(self, meta_node)

        self._actions = NodeActions(
            down=lambda msgs: self._manual_down(msgs),
            emit=lambda v: self._manual_emit(v),
            up=self.up,
        )

    # --- Private methods (promoted from closures) ---

    def _manual_down(self, messages: Messages) -> None:
        self._manual_emit_used = True
        self.down(messages)

    def _manual_emit(self, value: Any) -> None:
        self._manual_emit_used = True
        self._emit_auto_value(value)

    def _emit_to_sinks(self, msgs: Messages) -> None:
        if self._sinks is None:
            return
        if isinstance(self._sinks, set):
            # Snapshot: a sink callback may unsubscribe itself or others mid-iteration.
            # Iterating the live set would raise RuntimeError on mutation.
            snapshot = list(self._sinks)
            for s in snapshot:
                s(msgs)
        else:
            self._sinks(msgs)

    def _handle_local_lifecycle(self, messages: Messages) -> None:
        lock = self._cache_lock
        for m in messages:
            t = m[0]
            if t is MessageType.DATA:
                if lock is not None:
                    with lock:
                        self._cached = m[1]  # type: ignore[misc]
                else:
                    self._cached = m[1]  # type: ignore[misc]
            if t is MessageType.INVALIDATE:
                # GRAPHREFLY-SPEC §1.2: clear cached state; do not auto-emit from here.
                if self._cleanup is not None:
                    cb = self._cleanup
                    self._cleanup = None
                    cb()
                if lock is not None:
                    with lock:
                        self._cached = None
                else:
                    self._cached = None
                self._last_dep_values = None
            self._status = _status_after_message(self._status, m)
            if t is MessageType.COMPLETE or t is MessageType.ERROR:
                self._terminal = True
            if t is MessageType.TEARDOWN:
                if self._reset_on_teardown:
                    if lock is not None:
                        with lock:
                            self._cached = None
                    else:
                        self._cached = None
                try:
                    for meta_node in self._meta.values():
                        with suppress(Exception):
                            meta_node.down([(MessageType.TEARDOWN,)])
                finally:
                    self._disconnect_upstream()
                    self._stop_producer()

    def _can_skip_dirty(self) -> bool:
        return self._sink_count == 1 and self._single_dep_sink_count == 1

    def _emit_auto_value(self, value: Any) -> None:
        # Note: the read-compare-write on _cached looks like a TOCTOU race, but
        # callers always hold the subgraph RLock (via _run_fn or down), which
        # serializes all writes. _cache_lock only guards get() reads from outside.
        was_dirty = self._status == "dirty"
        lock = self._cache_lock
        if lock is not None:
            with lock:
                cached_snapshot = self._cached
        else:
            cached_snapshot = self._cached
        unchanged = self._equals(cached_snapshot, value)
        if unchanged:
            if was_dirty:
                self.down([(MessageType.RESOLVED,)])
            else:
                self.down([(MessageType.DIRTY,), (MessageType.RESOLVED,)])
            return
        if lock is not None:
            with lock:
                self._cached = cast("T", value)
        else:
            self._cached = cast("T", value)
        if was_dirty:
            self.down([(MessageType.DATA, value)])
        else:
            self.down([(MessageType.DIRTY,), (MessageType.DATA, value)])

    def _run_fn_body(self) -> None:
        if self._terminal and not self._resubscribable:
            return

        try:
            dep_values = [d.get() for d in self._deps]
            # Identity check BEFORE cleanup: if all dep values are unchanged,
            # skip cleanup+fn entirely so effect nodes don't teardown/restart on no-op.
            prev = self._last_dep_values
            n = len(dep_values)
            if (
                n > 0
                and prev is not None
                and len(prev) == n
                and all(dep_values[i] is prev[i] for i in range(n))
            ):
                if self._status == "dirty":
                    self.down([(MessageType.RESOLVED,)])
                return
            if self._cleanup is not None:
                self._cleanup()
                self._cleanup = None
            self._manual_emit_used = False
            self._last_dep_values = dep_values
            out = self._fn(dep_values, self._actions)  # type: ignore[misc]
            if _is_cleanup_fn(out):
                self._cleanup = out
                return
            if self._manual_emit_used:
                return
            if out is None:
                return
            self._emit_auto_value(out)
        except Exception as err:
            self.down([(MessageType.ERROR, err)])

    def _run_fn(self) -> None:
        if self._fn is None:
            return
        # Suppress re-entrant recompute while wiring upstream deps (TS connect order).
        if self._connecting:
            return
        if self._thread_safe:
            with acquire_subgraph_write_lock_with_defer(self):
                self._run_fn_body()
        else:
            self._run_fn_body()

    def _on_dep_dirty(self, index: int) -> None:
        was_dirty = self._dep_dirty_mask.has(index)
        self._dep_dirty_mask.set(index)
        self._dep_settled_mask.clear(index)
        if not was_dirty:
            self.down([(MessageType.DIRTY,)])

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
            self.down([(MessageType.COMPLETE,)])

    def _handle_dep_messages(self, index: int, messages: Messages) -> None:
        for msg in messages:
            t = msg[0]
            if self._fn is None:
                if t is MessageType.COMPLETE and len(self._deps) > 1:
                    self._dep_complete_mask.set(index)
                    self._maybe_complete_from_deps()
                    continue
                self.down([msg])
                continue
            if t is MessageType.DIRTY:
                self._on_dep_dirty(index)
                continue
            if t is MessageType.DATA or t is MessageType.RESOLVED:
                self._on_dep_settled(index)
                continue
            if t is MessageType.COMPLETE:
                self._dep_complete_mask.set(index)
                self._dep_dirty_mask.clear(index)
                self._dep_settled_mask.clear(index)
                if self._dep_dirty_mask.any() and self._dep_settled_mask.covers(
                    self._dep_dirty_mask
                ):
                    self._dep_dirty_mask.reset()
                    self._dep_settled_mask.reset()
                    self._run_fn()
                elif not self._dep_dirty_mask.any() and self._status == "dirty":
                    # D2: dep went DIRTY→COMPLETE without DATA — node was marked
                    # dirty but no settlement came.  Recompute so downstream
                    # gets RESOLVED (value unchanged) or DATA (value changed).
                    self._dep_settled_mask.reset()
                    self._run_fn()
                self._maybe_complete_from_deps()
                continue
            if t is MessageType.ERROR:
                self.down([msg])
                continue
            if (
                t is MessageType.INVALIDATE
                or t is MessageType.TEARDOWN
                or t is MessageType.PAUSE
                or t is MessageType.RESUME
            ):
                self.down([msg])
                continue
            # Forward unknown message types
            self.down([msg])

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

    # --- Public interface ---

    def _subscribe_body(
        self,
        sink: Callable[[Messages], None],
        hints: SubscribeHints | None,
    ) -> None:
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

    def _unsubscribe_body(self, sink: Callable[[Messages], None]) -> None:
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

    def subscribe(
        self,
        sink: Callable[[Messages], None],
        hints: SubscribeHints | None = None,
    ) -> Callable[[], None]:
        if self._thread_safe:
            with acquire_subgraph_write_lock_with_defer(self):
                self._subscribe_body(sink, hints)
        else:
            self._subscribe_body(sink, hints)

        removed = False

        if self._thread_safe:

            def unsubscribe() -> None:
                nonlocal removed
                with acquire_subgraph_write_lock_with_defer(self):
                    if removed:
                        return
                    removed = True
                    self._unsubscribe_body(sink)

        else:

            def unsubscribe() -> None:
                nonlocal removed
                if removed:
                    return
                removed = True
                self._unsubscribe_body(sink)

        return unsubscribe

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def status(self) -> NodeStatus:
        return self._status

    @property
    def meta(self) -> Mapping[str, NodeImpl[Any]]:
        return MappingProxyType(self._meta)

    def get(self) -> T | None:
        lock = self._cache_lock
        if lock is not None:
            with lock:
                return self._cached
        return self._cached

    def _down_body(self, messages: Messages, sg_lock: object | None) -> None:
        lifecycle_messages = messages
        sink_messages = messages
        if self._terminal and not self._resubscribable:
            terminal_passthrough = [
                m
                for m in messages
                if m[0] is MessageType.TEARDOWN or m[0] is MessageType.INVALIDATE
            ]
            if not terminal_passthrough:
                return
            lifecycle_messages = terminal_passthrough
            sink_messages = terminal_passthrough
        self._handle_local_lifecycle(lifecycle_messages)
        if self._can_skip_dirty():
            has_phase2 = False
            for m in sink_messages:
                t = m[0]
                if t is MessageType.DATA or t is MessageType.RESOLVED:
                    has_phase2 = True
                    break
            if has_phase2:
                filtered = [m for m in sink_messages if m[0] is not MessageType.DIRTY]
                if filtered:
                    emit_with_batch(
                        self._emit_to_sinks,
                        filtered,
                        strategy="partition",
                        defer_when="depth",
                        subgraph_lock=sg_lock,
                    )
                return
        emit_with_batch(
            self._emit_to_sinks,
            sink_messages,
            strategy="partition",
            defer_when="depth",
            subgraph_lock=sg_lock,
        )

    def down(self, messages: Messages) -> None:
        if not messages:
            return
        if self._thread_safe:
            with acquire_subgraph_write_lock_with_defer(self):
                self._down_body(messages, self)
        else:
            self._down_body(messages, None)

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
        if self._thread_safe:
            with acquire_subgraph_write_lock_with_defer(self):
                self._disconnect_upstream()
        else:
            self._disconnect_upstream()

    def __or__(self, other: object) -> Any:
        """Pipe: ``left | op`` with unary ``(Node) -> Node`` (GRAPHREFLY-SPEC §4.1)."""
        if not callable(other):
            return NotImplemented
        cast_other = cast("Callable[[NodeImpl[Any]], NodeImpl[Any]]", other)
        return cast_other(self)


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
    if "threadSafe" in opts and "thread_safe" not in opts:
        opts["thread_safe"] = opts.pop("threadSafe")

    return NodeImpl(deps, fn, opts)


# Public alias for type hints
Node = NodeImpl

__all__ = ["Node", "NodeActions", "NodeFn", "NodeImpl", "NodeStatus", "SubscribeHints", "node"]
