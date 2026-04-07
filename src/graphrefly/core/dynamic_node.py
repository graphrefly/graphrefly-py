"""``dynamic_node`` — runtime dep tracking with diamond resolution.

Unlike ``node()`` where deps are fixed at construction, ``dynamic_node``
discovers deps at runtime via a tracking ``get()`` proxy. After each
recompute, deps are diffed: new deps are connected, removed deps are
disconnected, and bitmasks are rebuilt. Kept deps retain their
subscriptions (no teardown/reconnect churn).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from graphrefly.core.guard import MutationRecord

from graphrefly.core.node import _SENTINEL
from graphrefly.core.protocol import (
    Messages,
    MessageType,
    down_with_batch,
    message_tier,
    propagates_to_meta,
)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

type DynGet = Callable[..., Any]  # (dep: Node) -> value | None
type DynamicNodeFn[T] = Callable[[DynGet], T]

# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def dynamic_node[T](
    fn: DynamicNodeFn[T],
    *,
    name: str | None = None,
    equals: Callable[[Any, Any], bool] | None = None,
    meta: dict[str, Any] | None = None,
    guard: Callable[[Any, str], bool] | None = None,
    on_message: Callable[[Any, int, Any], bool] | None = None,
    on_resubscribe: Callable[[], None] | None = None,
    complete_when_deps_complete: bool = True,
    describe_kind: str | None = None,
    resubscribable: bool = False,
    reset_on_teardown: bool = False,
    thread_safe: bool = True,
) -> DynamicNodeImpl[T]:
    """Create a node with runtime dep tracking.

    Deps are discovered each time the compute function runs by tracking
    which nodes are passed to the ``get()`` proxy.

    After each recompute:

    - New deps (not in previous set) are subscribed.
    - Removed deps (not in current set) are unsubscribed.
    - Kept deps retain their existing subscriptions.

    The node participates fully in diamond resolution via the standard
    two-phase DIRTY/RESOLVED protocol.

    Example::

        cond = state(True)
        a = state(1)
        b = state(2)

        d = dynamic_node(lambda get: get(a) if get(cond) else get(b))
    """
    return DynamicNodeImpl(
        fn,
        name=name,
        equals=equals or (lambda a, b: a is b),
        meta=meta,
        guard=guard,
        on_message=on_message,
        on_resubscribe=on_resubscribe,
        complete_when_deps_complete=complete_when_deps_complete,
        describe_kind=describe_kind,
        resubscribable=resubscribable,
        reset_on_teardown=reset_on_teardown,
        thread_safe=thread_safe,
    )


# ---------------------------------------------------------------------------
# Actions object (for on_message handler)
# ---------------------------------------------------------------------------


class _DynamicNodeActions:
    """Imperative ``actions`` object exposed to on_message handlers."""

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


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


class DynamicNodeImpl[T]:
    """Internal implementation of ``dynamic_node``."""

    __slots__ = (
        "__weakref__",
        "_actions",
        "_auto_complete",
        "_cache_lock",
        "_cached",
        "_complete_bits",
        "_connected",
        "_dep_index_map",
        "_dep_unsubs",
        "_deps",
        "_describe_kind",
        "_dirty_bits",
        "_equals",
        "_fn",
        "_guard",
        "_inspector_hook",
        "_last_mutation",
        "_meta",
        "_name",
        "_on_message",
        "_on_resubscribe",
        "_resubscribable",
        "_reset_on_teardown",
        "_rewiring",
        "_settled_bits",
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
        fn: DynamicNodeFn[T],
        *,
        name: str | None,
        equals: Callable[[Any, Any], bool],
        meta: dict[str, Any] | None,
        guard: Callable[[Any, str], bool] | None,
        on_message: Callable[[Any, int, Any], bool] | None,
        on_resubscribe: Callable[[], None] | None,
        complete_when_deps_complete: bool,
        describe_kind: str | None,
        resubscribable: bool,
        reset_on_teardown: bool,
        thread_safe: bool,
    ) -> None:
        # Deferred import to avoid circular dependency
        from graphrefly.core.node import node as create_node
        from graphrefly.core.subgraph_locks import ensure_registered, union_nodes

        self._fn = fn
        self._name = name
        self._equals = equals
        self._guard = guard
        self._on_message = on_message
        self._on_resubscribe = on_resubscribe
        self._auto_complete = complete_when_deps_complete
        self._describe_kind = describe_kind
        self._last_mutation: MutationRecord | None = None
        self._resubscribable = resubscribable
        self._reset_on_teardown = reset_on_teardown
        self._thread_safe = bool(thread_safe)
        self._inspector_hook: Callable[[dict[str, Any]], None] | None = None

        self._cached: Any = _SENTINEL
        self._status: str = "disconnected"
        self._terminal = False
        self._connected = False
        self._rewiring = False

        # Thread safety
        self._cache_lock = threading.Lock() if self._thread_safe else None

        # Dynamic deps tracking
        self._deps: list[Any] = []
        self._dep_unsubs: list[Callable[[], None]] = []
        self._dep_index_map: dict[int, int] = {}  # id(dep) -> index
        self._dirty_bits: set[int] = set()
        self._settled_bits: set[int] = set()
        self._complete_bits: set[int] = set()

        # Sinks
        self._sinks: Callable[[Messages], None] | set[Callable[[Messages], None]] | None = None
        self._sink_count = 0
        self._single_dep_sink_count = 0
        self._single_dep_sinks: set[Callable[..., Any]] = set()

        # Build companion meta nodes (same pattern as NodeImpl)
        built_meta: dict[str, Any] = {}
        for k, v in (meta or {}).items():
            meta_opts: dict[str, Any] = {
                "initial": v,
                "name": f"{name or 'dynamicNode'}:meta:{k}",
                "describe_kind": "state",
                "thread_safe": self._thread_safe,
            }
            if guard is not None:
                meta_opts["guard"] = guard
            built_meta[k] = create_node(**meta_opts)
        self._meta: Mapping[str, Any] = MappingProxyType(built_meta)

        # Register with subgraph lock registry
        if self._thread_safe:
            ensure_registered(self)
            for meta_node in self._meta.values():
                union_nodes(self, meta_node)

        # Actions object for on_message handler
        self._actions = _DynamicNodeActions(
            down=lambda msgs: self._down_internal(msgs),
            emit=lambda v: self._down_auto_value(v),
            up=lambda msgs: self.up(msgs, internal=True),
        )

    # --- Public interface (Node protocol) ---

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

    @property
    def v(self) -> None:
        """Versioning not yet supported on DynamicNodeImpl."""
        return None

    def has_guard(self) -> bool:
        return self._guard is not None

    def allows_observe(self, actor: Any = None) -> bool:
        if self._guard is None:
            return True
        from graphrefly.core.guard import normalize_actor

        a = normalize_actor(actor)
        return bool(self._guard(a, "observe"))

    def get(self) -> T | None:
        lock = self._cache_lock
        if lock is not None:
            with lock:
                v = self._cached
        else:
            v = self._cached
        return None if v is _SENTINEL else v

    def down(
        self,
        messages: Messages,
        *,
        actor: Any = None,
        internal: bool = False,
        guard_action: str = "write",
        **_kwargs: Any,
    ) -> None:
        if not messages:
            return
        if self._thread_safe:
            from graphrefly.core.subgraph_locks import acquire_subgraph_write_lock_with_defer

            with acquire_subgraph_write_lock_with_defer(self):
                if not internal and self._guard is not None:
                    from graphrefly.core.guard import GuardDenied, normalize_actor, record_mutation

                    a = normalize_actor(actor)
                    if not self._guard(a, guard_action):
                        raise GuardDenied(a, self._name or "<unnamed>", guard_action)
                    if guard_action == "write":
                        self._last_mutation = record_mutation(a)
                self._down_internal(messages)
        else:
            if not internal and self._guard is not None:
                from graphrefly.core.guard import GuardDenied, normalize_actor, record_mutation

                a = normalize_actor(actor)
                if not self._guard(a, guard_action):
                    raise GuardDenied(a, self._name or "<unnamed>", guard_action)
                if guard_action == "write":
                    self._last_mutation = record_mutation(a)
            self._down_internal(messages)

    def subscribe(
        self,
        sink: Callable[[Messages], None],
        hints: Any = None,
        *,
        actor: Any = None,
        **_kwargs: Any,
    ) -> Callable[[], None]:
        check_actor = actor or (getattr(hints, "actor", None) if hints else None)
        if check_actor is not None and self._guard is not None:
            from graphrefly.core.guard import GuardDenied, normalize_actor

            a = normalize_actor(check_actor)
            if not self._guard(a, "observe"):
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
        hints: Any,
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

        # Track sink counts
        self._sink_count += 1
        h_single = getattr(hints, "single_dep", False) if hints else False
        if h_single:
            self._single_dep_sink_count += 1
            self._single_dep_sinks.add(sink)

        if self._sinks is None:
            self._sinks = sink
        elif callable(self._sinks) and not isinstance(self._sinks, set):
            self._sinks = {self._sinks, sink}
        else:
            assert isinstance(self._sinks, set)
            self._sinks.add(sink)

        if not self._connected:
            self._connect()

        removed = False

        if self._thread_safe:
            from graphrefly.core.subgraph_locks import acquire_subgraph_write_lock_with_defer

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

    def _unsubscribe_body(self, sink: Callable[[Messages], None]) -> None:
        self._sink_count -= 1
        if sink in self._single_dep_sinks:
            self._single_dep_sink_count -= 1
            self._single_dep_sinks.discard(sink)

        if self._sinks is None:
            return
        if callable(self._sinks) and not isinstance(self._sinks, set):
            if self._sinks is sink:
                self._sinks = None
        else:
            assert isinstance(self._sinks, set)
            self._sinks.discard(sink)
            if len(self._sinks) == 1:
                (only,) = self._sinks
                self._sinks = only
            elif len(self._sinks) == 0:
                self._sinks = None
        if self._sinks is None:
            self._disconnect()

    def up(
        self,
        messages: Messages,
        *,
        actor: Any = None,
        internal: bool = False,
        guard_action: str = "write",
        **_kwargs: Any,
    ) -> None:
        """Send messages upstream to currently-tracked deps."""
        if not self._deps:
            return
        if not internal and self._guard is not None:
            from graphrefly.core.guard import GuardDenied, normalize_actor, record_mutation

            a = normalize_actor(actor)
            if not self._guard(a, guard_action):
                raise GuardDenied(a, self._name or "<unnamed>", guard_action)
            if guard_action == "write":
                self._last_mutation = record_mutation(a)
        for dep in self._deps:
            u = getattr(dep, "up", None)
            if u is not None:
                u(messages, internal=internal)

    def unsubscribe(self) -> None:
        """Disconnect from all upstream deps."""
        self._disconnect()

    def __or__(self, other: object) -> Any:
        if not callable(other):
            return NotImplemented
        return other(self)

    # --- Inspector hook ---

    def _set_inspector_hook(
        self, hook: Callable[[dict[str, Any]], None] | None
    ) -> Callable[[], None]:
        """Internal inspector hook attach/detach for graph observability."""
        prev = self._inspector_hook
        self._inspector_hook = hook

        def dispose() -> None:
            if self._inspector_hook is hook:
                self._inspector_hook = prev

        return dispose

    # --- Private methods ---

    def _down_to_sinks(self, messages: Messages) -> None:
        if self._sinks is None:
            return
        if callable(self._sinks) and not isinstance(self._sinks, set):
            self._sinks(messages)
            return
        snapshot = list(self._sinks)
        for s in snapshot:
            s(messages)

    def _can_skip_dirty(self) -> bool:
        return self._sink_count == 1 and self._single_dep_sink_count == 1

    def _down_internal(self, messages: Messages) -> None:
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

        # singleDep DIRTY skip optimization
        if self._can_skip_dirty():
            has_phase2 = any(message_tier(m[0]) == 2 for m in messages)
            if has_phase2:
                filtered = [m for m in messages if m[0] is not MessageType.DIRTY]
                if filtered:
                    down_with_batch(self._down_to_sinks, filtered)
                return

        down_with_batch(self._down_to_sinks, messages)

    def _handle_local_lifecycle(self, messages: Messages) -> None:
        lock = self._cache_lock
        for m in messages:
            t = m[0]
            if t is MessageType.DATA:
                val = m[1] if len(m) > 1 else None
                if lock is not None:
                    with lock:
                        self._cached = val
                else:
                    self._cached = val
            if t is MessageType.INVALIDATE:
                if lock is not None:
                    with lock:
                        self._cached = _SENTINEL
                else:
                    self._cached = _SENTINEL
                self._status = "dirty"
            if t is MessageType.DATA:
                self._status = "settled"
            elif t is MessageType.RESOLVED:
                self._status = "resolved"
            elif t is MessageType.DIRTY:
                self._status = "dirty"
            elif t is MessageType.COMPLETE:
                self._status = "completed"
                self._terminal = True
            elif t is MessageType.ERROR:
                self._status = "errored"
                self._terminal = True
            if t is MessageType.TEARDOWN:
                if self._reset_on_teardown:
                    if lock is not None:
                        with lock:
                            self._cached = _SENTINEL
                    else:
                        self._cached = _SENTINEL
                try:
                    self._propagate_to_meta(t)
                finally:
                    self._disconnect()
            # Propagate other meta-eligible signals (centralized in protocol.py).
            if t is not MessageType.TEARDOWN and propagates_to_meta(t):
                self._propagate_to_meta(t)

    def _propagate_to_meta(self, t: MessageType) -> None:
        """Propagate a signal to all companion meta nodes (best-effort)."""
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
            msgs: Messages = (
                [(MessageType.RESOLVED,)]
                if was_dirty
                else [(MessageType.DIRTY,), (MessageType.RESOLVED,)]
            )
            self._down_internal(msgs)
            return
        if lock is not None:
            with lock:
                self._cached = value
        else:
            self._cached = value
        msgs = (
            [(MessageType.DATA, value)]
            if was_dirty
            else [(MessageType.DIRTY,), (MessageType.DATA, value)]
        )
        self._down_internal(msgs)

    def _connect(self) -> None:
        if self._connected:
            return
        self._connected = True
        self._status = "settled"
        self._dirty_bits.clear()
        self._settled_bits.clear()
        self._complete_bits.clear()
        self._run_fn()

    def _disconnect(self) -> None:
        if not self._connected:
            return
        for unsub in self._dep_unsubs:
            unsub()
        self._dep_unsubs = []
        self._deps = []
        self._dep_index_map.clear()
        self._dirty_bits.clear()
        self._settled_bits.clear()
        self._complete_bits.clear()
        self._connected = False
        self._status = "disconnected"

    def _run_fn(self) -> None:
        if self._terminal and not self._resubscribable:
            return
        if self._rewiring:
            return

        tracked_deps: list[Any] = []
        tracked_ids: set[int] = set()

        def get(dep: Any) -> Any:
            dep_id = id(dep)
            if dep_id not in tracked_ids:
                tracked_ids.add(dep_id)
                tracked_deps.append(dep)
            return dep.get()

        try:
            result = self._fn(get)

            # Inspector hook: collect dep values BEFORE rewire (pre-rewire deps
            # show what triggered recompute, matching TS semantics)
            if self._inspector_hook is not None:
                dep_values = [d.get() for d in self._deps]
                self._inspector_hook({"kind": "run", "dep_values": dep_values})

            self._rewire(tracked_deps)

            if result is None:
                return
            self._down_auto_value(result)
        except Exception as err:
            self._down_internal([(MessageType.ERROR, err)])

    def _rewire(self, new_deps: list[Any]) -> None:
        self._rewiring = True
        try:
            old_map = self._dep_index_map
            new_map: dict[int, int] = {}
            new_unsubs: list[Callable[[], None]] = []

            for i, dep in enumerate(new_deps):
                dep_id = id(dep)
                new_map[dep_id] = i
                old_idx = old_map.get(dep_id)
                if old_idx is not None:
                    # Kept dep — reuse subscription
                    new_unsubs.append(self._dep_unsubs[old_idx])
                    self._dep_unsubs[old_idx] = lambda: None
                else:
                    # New dep — subscribe
                    idx = i
                    unsub = dep.subscribe(
                        lambda msgs, _idx=idx: self._handle_dep_messages(_idx, msgs)
                    )
                    new_unsubs.append(unsub)
                    # Union with new dep for thread safety
                    if self._thread_safe:
                        from graphrefly.core.subgraph_locks import union_nodes

                        union_nodes(self, dep)

            # Disconnect removed deps
            for dep_id, old_idx in old_map.items():
                if dep_id not in new_map:
                    self._dep_unsubs[old_idx]()

            self._deps = new_deps
            self._dep_unsubs = new_unsubs
            self._dep_index_map = new_map
            self._dirty_bits.clear()
            self._settled_bits.clear()

            # Preserve complete bits for deps still present
            new_complete: set[int] = set()
            for old_idx in self._complete_bits:
                # Find dep at old_idx and check if in new_map
                for dep_id, idx in old_map.items():
                    if idx == old_idx and dep_id in new_map:
                        new_complete.add(new_map[dep_id])
                        break
            self._complete_bits = new_complete
        finally:
            self._rewiring = False

    def _handle_dep_messages(self, index: int, messages: Messages) -> None:
        if self._rewiring:
            return

        for msg in messages:
            # Inspector hook
            if self._inspector_hook is not None:
                self._inspector_hook({"kind": "dep_message", "dep_index": index, "message": msg})

            t = msg[0]

            # User-defined message handler gets first look
            if self._on_message is not None:
                try:
                    if self._on_message(msg, index, self._actions):
                        continue
                except Exception as err:
                    self._down_internal([(MessageType.ERROR, err)])
                    return

            if t is MessageType.DIRTY:
                self._dirty_bits.add(index)
                self._settled_bits.discard(index)
                if len(self._dirty_bits) == 1:
                    down_with_batch(self._down_to_sinks, [(MessageType.DIRTY,)])
                continue
            if t is MessageType.DATA or t is MessageType.RESOLVED:
                if index not in self._dirty_bits:
                    self._dirty_bits.add(index)
                    down_with_batch(self._down_to_sinks, [(MessageType.DIRTY,)])
                self._settled_bits.add(index)
                if self._all_dirty_settled():
                    self._dirty_bits.clear()
                    self._settled_bits.clear()
                    self._run_fn()
                continue
            if t is MessageType.COMPLETE:
                self._complete_bits.add(index)
                self._dirty_bits.discard(index)
                self._settled_bits.discard(index)
                if self._all_dirty_settled():
                    self._dirty_bits.clear()
                    self._settled_bits.clear()
                    self._run_fn()
                if self._auto_complete and len(self._complete_bits) >= len(self._deps) > 0:
                    self._down_internal([(MessageType.COMPLETE,)])
                continue
            if t is MessageType.ERROR:
                self._down_internal([msg])
                continue
            # INVALIDATE, TEARDOWN, PAUSE, RESUME — pass through
            self._down_internal([msg])

    def _all_dirty_settled(self) -> bool:
        if not self._dirty_bits:
            return False
        return self._dirty_bits <= self._settled_bits
