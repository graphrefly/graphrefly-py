"""``NodeBase`` — abstract base class shared by ``NodeImpl`` (static deps) and
``DynamicNodeImpl`` (runtime-tracked deps).

Responsibilities (shared):
- Identity (name, describeKind, meta, guard, versioning)
- Cache + status lifecycle (``_cached``, ``_status``, ``_terminal``)
- Sink storage (null / single / set fast paths)
- ``subscribe()`` with START handshake + first-subscriber activation
- ``_down_internal`` → ``_down_to_sinks`` delivery pipeline (via ``down_with_batch``)
- ``_down_auto_value`` (value → protocol framing with equals)
- ``_handle_local_lifecycle`` (cached/status/terminal updates + meta propagation)

Subclass hooks (abstract):
- ``_on_activate()`` — called when sink_count transitions 0 → 1
- ``_do_deactivate()`` — cleanup on deactivation (at-most-once, guarded by ``_on_deactivate``)
- ``up()`` / ``unsubscribe()`` — dep-iteration specifics

See GRAPHREFLY-SPEC §2 and COMPOSITION-GUIDE §1 for protocol contracts.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from contextlib import suppress
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from graphrefly.core.guard import MutationRecord

from graphrefly.core.guard import (
    Actor,
    GuardAction,
    GuardDenied,
    normalize_actor,
    record_mutation,
)
from graphrefly.core.protocol import (
    Messages,
    MessageType,
    down_with_batch,
    message_tier,
    propagates_to_meta,
)

# Internal sentinel: "no cached value has been set or emitted."
# Distinct from None so that None can be a valid emitted value.
_SENTINEL = object()

NO_VALUE = _SENTINEL

# --- Status ---

type NodeStatus = str  # structural: same strings as TS NodeStatus

# Open wire set: first element may be MessageType or any hashable tag.
type Message = tuple[Any, Any] | tuple[Any]


def _status_after_message(status: NodeStatus, msg: Message) -> NodeStatus:
    """Returns the post-message status. START is informational and does not transition."""
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


# --- Cleanup result ---

_CLEANUP_RESULT = "__graphrefly_cleanup_result__"


def cleanup_result(cleanup: Callable[[], None], value: Any = _SENTINEL) -> dict[str, Any]:
    """Create a branded cleanup-result wrapper."""
    r: dict[str, Any] = {_CLEANUP_RESULT: True, "cleanup": cleanup}
    if value is not _SENTINEL:
        r["value"] = value
    return r


def _is_cleanup_result(value: object) -> bool:
    return isinstance(value, dict) and value.get(_CLEANUP_RESULT) is True


def _is_cleanup_fn(value: object) -> bool:
    return callable(value)


# --- BitSet ---


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

    def set_all(self) -> None:
        self._bits = (1 << self._width) - 1


def _create_bit_set(size: int) -> _BitSet:
    return _BitSet(size)


# --- NodeActions ---


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


# --- SubscribeHints ---


class SubscribeHints:
    """Hints passed to :meth:`NodeBase.subscribe` to enable optimizations."""

    __slots__ = ("single_dep",)

    def __init__(self, *, single_dep: bool = False) -> None:
        self.single_dep = single_dep


# --- NodeBase ---


class NodeBase[T](ABC):
    """Abstract base for every node in the graph.

    Both ``NodeImpl`` (static deps) and ``DynamicNodeImpl`` (runtime-tracked
    deps) extend this to share subscribe/sink/lifecycle machinery.

    ROM/RAM rule (GRAPHREFLY-SPEC §2.2): state nodes (no fn) preserve
    ``_cached`` across disconnect — intrinsic, non-volatile. Compute nodes
    (derived, producer, dynamic) clear ``_cached`` on disconnect in their
    subclass ``_on_deactivate``.
    """

    __slots__ = (
        "__weakref__",
        "_actions",
        "_active",
        "_cache_lock",
        "_cached",
        "_describe_kind",
        "_equals",
        "_guard",
        "_inspector_hook",
        "_last_mutation",
        "_meta",
        "_name",
        "_on_message",
        "_on_resubscribe",
        "_resubscribable",
        "_reset_on_teardown",
        "_single_dep_sink_count",
        "_single_dep_sinks",
        "_sink_count",
        "_sinks",
        "_status",
        "_terminal",
        "_thread_safe",
    )

    def __init__(
        self,
        *,
        name: str | None = None,
        describe_kind: str | None = None,
        equals: Callable[[Any, Any], bool] | None = None,
        initial: Any = _SENTINEL,
        meta: dict[str, Any] | None = None,
        guard: Callable[[Any, str], bool] | None = None,
        on_message: Callable[[Any, int, Any], bool] | None = None,
        on_resubscribe: Callable[[], None] | None = None,
        resubscribable: bool = False,
        reset_on_teardown: bool = False,
        thread_safe: bool = True,
        **_extra: Any,
    ) -> None:
        import operator as _op

        self._name = name
        self._describe_kind = describe_kind
        self._equals: Callable[[Any, Any], bool] = equals or _op.is_
        self._resubscribable = resubscribable
        self._reset_on_teardown = reset_on_teardown
        self._on_resubscribe = on_resubscribe
        self._on_message = on_message
        self._thread_safe = bool(thread_safe)

        if guard is not None and not callable(guard):
            msg = "node option 'guard' must be callable or None"
            raise TypeError(msg)
        self._guard: Callable[[Actor, GuardAction], bool] | None = guard
        self._last_mutation: MutationRecord | None = None
        self._inspector_hook: Callable[[dict[str, Any]], None] | None = None

        # Cache / status
        self._cache_lock = threading.Lock() if self._thread_safe else None
        self._cached: Any = initial
        self._status: NodeStatus = "settled" if initial is not _SENTINEL else "disconnected"
        self._terminal = False
        self._active = False

        # Sinks
        self._sinks: Callable[[Messages], None] | set[Callable[[Messages], None]] | None = None
        self._sink_count = 0
        self._single_dep_sink_count = 0
        self._single_dep_sinks: set[Callable[..., Any]] = set()

        # Build companion meta nodes
        built_meta: dict[str, Any] = {}
        for k, v in (meta or {}).items():
            built_meta[k] = self._create_meta_node(k, v, name, guard)
        self._meta: Mapping[str, Any] = MappingProxyType(built_meta)

        # Register with subgraph locks
        if self._thread_safe:
            from graphrefly.core.subgraph_locks import ensure_registered, union_nodes

            ensure_registered(self)
            for meta_node in self._meta.values():
                union_nodes(self, meta_node)

        # Actions: created once, captures self methods.
        self._actions = NodeActions(
            down=lambda msgs: self._manual_down(msgs),
            emit=lambda v: self._manual_emit(v),
            up=lambda msgs: self._up_internal(msgs),
        )

    def _create_meta_node(
        self,
        key: str,
        initial_value: Any,
        parent_name: str | None,
        guard: Callable[[Any, str], bool] | None,
    ) -> Any:
        """Create a companion meta node. Deferred import to avoid circular deps."""
        from graphrefly.core.node import node as create_node

        meta_opts: dict[str, Any] = {
            "initial": initial_value,
            "name": f"{parent_name or 'node'}:meta:{key}",
            "describe_kind": "state",
            "thread_safe": self._thread_safe,
        }
        if guard is not None:
            meta_opts["guard"] = guard
        return create_node(**meta_opts)

    # --- Subclass hooks ---

    def _manual_down(self, messages: Messages) -> None:
        """Override in subclasses that track manual emit usage."""
        self._down_internal(messages)

    def _manual_emit(self, value: Any) -> None:
        """Override in subclasses that track manual emit usage."""
        self._down_auto_value(value)

    @abstractmethod
    def _up_internal(self, messages: Messages) -> None:
        """Send messages upstream (used by actions.up)."""

    @abstractmethod
    def _on_activate(self) -> None:
        """Called when sink_count transitions 0 → 1."""

    def _on_deactivate(self) -> None:
        """At-most-once deactivation guard.

        Both TEARDOWN (eager) and _unsubscribe_body (lazy) call this.
        The ``_active`` flag ensures ``_do_deactivate`` runs exactly once
        per activation cycle — subclasses need no idempotency guard.
        """
        if not self._active:
            return
        self._active = False
        self._do_deactivate()

    @abstractmethod
    def _do_deactivate(self) -> None:
        """Subclass hook: cleanup on deactivation (called at most once)."""

    @abstractmethod
    def _on_invalidate(self) -> None:
        """Called when INVALIDATE arrives, before _cached is cleared."""

    @abstractmethod
    def _on_teardown(self) -> None:
        """Called when TEARDOWN arrives, before _on_deactivate."""

    # --- Identity getters ---

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def status(self) -> str:
        return self._status

    @property
    def meta(self) -> Mapping[str, Any]:
        return self._meta

    @property
    def last_mutation(self) -> MutationRecord | None:
        return self._last_mutation

    def has_guard(self) -> bool:
        return self._guard is not None

    def allows_observe(self, actor: Any = None) -> bool:
        if self._guard is None:
            return True
        a = normalize_actor(actor)
        return bool(self._guard(cast("Actor", a), "observe"))

    def get(self) -> T | None:
        lock = self._cache_lock
        if lock is not None:
            with lock:
                v = self._cached
        else:
            v = self._cached
        return None if v is _SENTINEL else v

    def _set_inspector_hook(
        self, hook: Callable[[dict[str, Any]], None] | None
    ) -> Callable[[], None]:
        prev = self._inspector_hook
        self._inspector_hook = hook

        def dispose() -> None:
            if self._inspector_hook is hook:
                self._inspector_hook = prev

        return dispose

    def __or__(self, other: object) -> Any:
        if not callable(other):
            return NotImplemented
        return other(self)

    # --- Public transport ---

    def down(
        self,
        messages: Messages,
        *,
        actor: Any = None,
        internal: bool = False,
        guard_action: GuardAction = "write",
        **_kw: Any,
    ) -> None:
        if not messages:
            return
        if self._thread_safe:
            from graphrefly.core.subgraph_locks import acquire_subgraph_write_lock_with_defer

            with acquire_subgraph_write_lock_with_defer(self):
                if not internal:
                    self._guard_and_record(actor, guard_action)
                self._down_body(messages, self)
        else:
            if not internal:
                self._guard_and_record(actor, guard_action)
            self._down_body(messages, None)

    @abstractmethod
    def up(
        self,
        messages: Messages,
        *,
        actor: Any = None,
        internal: bool = False,
        guard_action: GuardAction = "write",
        **_kw: Any,
    ) -> None: ...

    @abstractmethod
    def unsubscribe(self) -> None: ...

    # --- Subscribe ---

    def subscribe(
        self,
        sink: Callable[[Messages], None],
        hints: SubscribeHints | None = None,
        *,
        actor: Any = None,
        **_kw: Any,
    ) -> Callable[[], None]:
        check_actor = actor or (getattr(hints, "actor", None) if hints else None)
        if check_actor is not None and self._guard is not None:
            a = normalize_actor(check_actor)
            if not self._guard(cast("Actor", a), "observe"):
                raise GuardDenied(a, self._name or "<unnamed>", "observe")

        if self._thread_safe:
            from graphrefly.core.subgraph_locks import acquire_subgraph_write_lock_with_defer

            with acquire_subgraph_write_lock_with_defer(self):
                return self._subscribe_body(sink, hints)
        else:
            return self._subscribe_body(sink, hints)

    def _subscribe_body(
        self,
        sink: Callable[[Messages], None],
        hints: SubscribeHints | None,
    ) -> Callable[[], None]:
        if self._terminal and self._resubscribable:
            self._terminal = False
            lock = self._cache_lock
            if lock is not None:
                with lock:
                    self._cached = _SENTINEL
            else:
                self._cached = _SENTINEL
            self._status = "disconnected"
            if self._on_resubscribe is not None:
                self._on_resubscribe()

        h = hints or SubscribeHints()
        self._sink_count += 1
        if h.single_dep:
            self._single_dep_sink_count += 1
            self._single_dep_sinks.add(sink)

        # §2.2 START handshake — delivered BEFORE registering sink in _sinks.
        # START is sent directly to the new sink (not via _down_to_sinks).
        # After START, the sink is registered so _on_activate's re-entrant
        # emissions via _down_to_sinks CAN reach it (this is intentional —
        # the sink already received START).
        #
        # Wire shape:
        #   SENTINEL cache → [(START,)]
        #   cached value   → [(START,), (DATA, cached)]
        #
        # Terminal nodes skip entirely — absence of START tells the subscriber
        # the stream is over (spec §2.2).
        if not self._terminal:
            lock = self._cache_lock
            if lock is not None:
                with lock:
                    cached = self._cached
            else:
                cached = self._cached
            if cached is _SENTINEL:
                start_msgs: Messages = [(MessageType.START,)]
            else:
                start_msgs = [(MessageType.START,), (MessageType.DATA, cached)]
            down_with_batch(sink, start_msgs)

        # Register sink AFTER START so re-entrant _down_to_sinks during
        # _on_activate can reach it.
        if self._sinks is None:
            self._sinks = sink
        elif isinstance(self._sinks, set):
            self._sinks.add(sink)
        else:
            self._sinks = {self._sinks, sink}

        # First subscriber triggers activation.
        if self._sink_count == 1 and not self._terminal:
            self._active = True
            self._on_activate()

        # After activation, infer status from cache state.
        if not self._terminal and self._status == "disconnected":
            if self._cached is _SENTINEL:
                self._status = "pending"
            else:
                self._status = "settled"

        removed = False

        if self._thread_safe:
            from graphrefly.core.subgraph_locks import acquire_subgraph_write_lock_with_defer

            def _unsub() -> None:
                nonlocal removed
                with acquire_subgraph_write_lock_with_defer(self):
                    if removed:
                        return
                    removed = True
                    self._unsubscribe_body(sink)
        else:

            def _unsub() -> None:
                nonlocal removed
                if removed:
                    return
                removed = True
                self._unsubscribe_body(sink)

        return _unsub

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
                (only,) = self._sinks
                self._sinks = only
            elif len(self._sinks) == 0:
                self._sinks = None
        elif self._sinks is sink:
            self._sinks = None

        if self._sinks is None:
            self._on_deactivate()

    # --- Down pipeline ---

    def _down_body(self, messages: Messages, sg_lock: object | None) -> None:
        sink_messages = messages
        if self._terminal and not self._resubscribable:
            terminal_passthrough = [
                m
                for m in messages
                if m[0] is MessageType.TEARDOWN or m[0] is MessageType.INVALIDATE
            ]
            if not terminal_passthrough:
                return
            sink_messages = terminal_passthrough
        self._handle_local_lifecycle(sink_messages)
        if self._can_skip_dirty():
            has_phase2 = any(message_tier(m[0]) == 3 for m in sink_messages)
            if has_phase2:
                filtered = [m for m in sink_messages if m[0] is not MessageType.DIRTY]
                if filtered:
                    down_with_batch(
                        self._down_to_sinks,
                        filtered,
                        strategy="partition",
                        defer_when="batching",
                        subgraph_lock=sg_lock,
                    )
                return
        down_with_batch(
            self._down_to_sinks,
            sink_messages,
            strategy="partition",
            defer_when="batching",
            subgraph_lock=sg_lock,
        )

    def _down_internal(self, messages: Messages) -> None:
        """Core outgoing dispatch without guard checks."""
        if not messages:
            return
        if self._terminal and not self._resubscribable:
            filtered = [
                m
                for m in messages
                if m[0] is MessageType.TEARDOWN or m[0] is MessageType.INVALIDATE
            ]
            if not filtered:
                return
            messages = filtered
        self._handle_local_lifecycle(messages)
        if self._can_skip_dirty():
            has_phase2 = any(message_tier(m[0]) == 3 for m in messages)
            if has_phase2:
                filtered = [m for m in messages if m[0] is not MessageType.DIRTY]
                if filtered:
                    down_with_batch(self._down_to_sinks, filtered)
                return
        down_with_batch(self._down_to_sinks, messages)

    def _can_skip_dirty(self) -> bool:
        return self._sink_count == 1 and self._single_dep_sink_count == 1

    def _down_to_sinks(self, messages: Messages) -> None:
        if self._sinks is None:
            return
        if isinstance(self._sinks, set):
            snapshot = list(self._sinks)
            for s in snapshot:
                s(messages)
        else:
            self._sinks(messages)

    def _handle_local_lifecycle(self, messages: Messages) -> None:
        lock = self._cache_lock
        for m in messages:
            t = m[0]
            if t is MessageType.DATA:
                if len(m) < 2:
                    continue
                if lock is not None:
                    with lock:
                        self._cached = m[1]
                else:
                    self._cached = m[1]
            if t is MessageType.INVALIDATE:
                self._on_invalidate()
                if lock is not None:
                    with lock:
                        self._cached = _SENTINEL
                else:
                    self._cached = _SENTINEL
            self._status = _status_after_message(self._status, m)
            if t is MessageType.COMPLETE or t is MessageType.ERROR:
                self._terminal = True
            if t is MessageType.TEARDOWN:
                if self._reset_on_teardown:
                    if lock is not None:
                        with lock:
                            self._cached = _SENTINEL
                    else:
                        self._cached = _SENTINEL
                self._on_teardown()
                try:
                    self._propagate_to_meta(t)
                finally:
                    # Force upstream disconnect immediately — don't wait for
                    # downstream to unsubscribe.  _on_deactivate's _active
                    # guard ensures _do_deactivate runs at most once.
                    self._on_deactivate()
            if t is not MessageType.TEARDOWN and propagates_to_meta(t):
                self._propagate_to_meta(t)

    def _propagate_to_meta(self, t: MessageType) -> None:
        for meta_node in self._meta.values():
            with suppress(Exception):
                meta_node.down([(t,)], internal=True)

    def _down_auto_value(self, value: Any) -> None:
        was_dirty = self._status == "dirty"
        lock = self._cache_lock
        if lock is not None:
            with lock:
                cached_snapshot = self._cached
        else:
            cached_snapshot = self._cached
        try:
            unchanged = cached_snapshot is not _SENTINEL and self._equals(cached_snapshot, value)
        except Exception as eq_err:
            wrapped = RuntimeError(f'Node "{self._name}": equals threw: {eq_err}')
            wrapped.__cause__ = eq_err
            self._down_internal([(MessageType.ERROR, wrapped)])
            return
        if unchanged:
            if was_dirty:
                self._down_internal([(MessageType.RESOLVED,)])
            else:
                self._down_internal([(MessageType.DIRTY,), (MessageType.RESOLVED,)])
            return
        if was_dirty:
            self._down_internal([(MessageType.DATA, value)])
        else:
            self._down_internal([(MessageType.DIRTY,), (MessageType.DATA, value)])

    # --- Guard helpers ---

    def _guard_and_record(self, actor: Any, guard_action: GuardAction) -> None:
        a = normalize_actor(actor)
        g = self._guard
        if g is not None and not g(cast("Actor", a), guard_action):
            raise GuardDenied(a, self._name or "<unnamed>", guard_action)
        if guard_action == "write":
            self._last_mutation = record_mutation(a)

    def _guard_observe_or_raise(self, actor: Any) -> None:
        g = self._guard
        if g is None:
            return
        a = normalize_actor(actor)
        if not g(cast("Actor", a), "observe"):
            raise GuardDenied(a, self._name or "<unnamed>", "observe")
