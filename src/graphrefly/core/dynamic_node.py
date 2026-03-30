"""``dynamic_node`` — runtime dep tracking with diamond resolution.

Unlike ``node()`` where deps are fixed at construction, ``dynamic_node``
discovers deps at runtime via a tracking ``get()`` proxy. After each
recompute, deps are diffed: new deps are connected, removed deps are
disconnected, and bitmasks are rebuilt. Kept deps retain their
subscriptions (no teardown/reconnect churn).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from graphrefly.core.protocol import MessageType, Messages, emit_with_batch

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
    resubscribable: bool = False,
    reset_on_teardown: bool = False,
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
        resubscribable=resubscribable,
        reset_on_teardown=reset_on_teardown,
    )


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


class DynamicNodeImpl[T]:
    """Internal implementation of ``dynamic_node``."""

    __slots__ = (
        "_cached",
        "_complete_bits",
        "_connected",
        "_dep_index_map",
        "_dep_unsubs",
        "_deps",
        "_dirty_bits",
        "_equals",
        "_fn",
        "_meta",
        "_name",
        "_resubscribable",
        "_reset_on_teardown",
        "_rewiring",
        "_settled_bits",
        "_sinks",
        "_status",
        "_terminal",
    )

    def __init__(
        self,
        fn: DynamicNodeFn[T],
        *,
        name: str | None,
        equals: Callable[[Any, Any], bool],
        resubscribable: bool,
        reset_on_teardown: bool,
    ) -> None:
        self._fn = fn
        self._name = name
        self._equals = equals
        self._resubscribable = resubscribable
        self._reset_on_teardown = reset_on_teardown

        self._cached: T | None = None
        self._status: str = "disconnected"
        self._terminal = False
        self._connected = False
        self._rewiring = False

        # Dynamic deps tracking
        self._deps: list[Any] = []
        self._dep_unsubs: list[Callable[[], None]] = []
        self._dep_index_map: dict[int, int] = {}  # id(dep) -> index
        self._dirty_bits: set[int] = set()
        self._settled_bits: set[int] = set()
        self._complete_bits: set[int] = set()

        # Sinks
        self._sinks: Callable[[Messages], None] | set[Callable[[Messages], None]] | None = None

        # Meta (empty — dynamic_node doesn't support meta)
        self._meta: Mapping[str, Any] = MappingProxyType({})

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
    def last_mutation(self) -> None:
        return None

    def has_guard(self) -> bool:
        return False

    def allows_observe(self, actor: Any = None) -> bool:
        return True

    def get(self) -> T | None:
        return self._cached

    def down(self, messages: Messages, **_kwargs: Any) -> None:
        self._down_internal(messages)

    def subscribe(
        self,
        sink: Callable[[Messages], None],
        hints: Any = None,
        **_kwargs: Any,
    ) -> Callable[[], None]:
        if self._terminal and self._resubscribable:
            self._terminal = False
            self._status = "disconnected"

        if self._sinks is None:
            self._sinks = sink
        elif callable(self._sinks) and not isinstance(self._sinks, set):
            self._sinks = {self._sinks, sink}
        else:
            self._sinks.add(sink)  # type: ignore[union-attr]

        if not self._connected:
            self._connect()

        removed = False

        def unsubscribe() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
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

        return unsubscribe

    def up(self, messages: Messages, **_kwargs: Any) -> None:
        """No-op — dynamicNode doesn't support up()."""

    def unsubscribe(self) -> None:
        """Disconnect from all upstream deps."""
        self._disconnect()

    def __or__(self, other: object) -> Any:
        if not callable(other):
            return NotImplemented
        return other(self)

    # --- Private methods ---

    def _emit_to_sinks(self, messages: Messages) -> None:
        if self._sinks is None:
            return
        if callable(self._sinks) and not isinstance(self._sinks, set):
            self._sinks(messages)
            return
        snapshot = list(self._sinks)
        for s in snapshot:
            s(messages)

    def _down_internal(self, messages: Messages) -> None:
        if not messages:
            return
        if self._terminal and not self._resubscribable:
            filtered = [
                m for m in messages
                if m[0] is MessageType.TEARDOWN or m[0] is MessageType.INVALIDATE
            ]
            if not filtered:
                return
            messages = filtered
        self._handle_local_lifecycle(messages)
        self._emit_to_sinks(messages)

    def _handle_local_lifecycle(self, messages: Messages) -> None:
        for m in messages:
            t = m[0]
            if t is MessageType.DATA:
                self._cached = m[1]
            if t is MessageType.INVALIDATE:
                self._cached = None
            if t is MessageType.DATA or t is MessageType.RESOLVED:
                self._status = "settled"
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
                    self._cached = None
                self._disconnect()

    def _emit_auto_value(self, value: Any) -> None:
        was_dirty = self._status == "dirty"
        unchanged = self._equals(self._cached, value)
        if unchanged:
            msgs: Messages = (
                [(MessageType.RESOLVED,)]
                if was_dirty
                else [(MessageType.DIRTY,), (MessageType.RESOLVED,)]
            )
            self._down_internal(msgs)
            return
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
            self._rewire(tracked_deps)
            if result is None:
                return
            self._emit_auto_value(result)
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
                    unsub = dep.subscribe(lambda msgs, _idx=idx: self._handle_dep_messages(_idx, msgs))
                    new_unsubs.append(unsub)

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
            t = msg[0]
            if t is MessageType.DIRTY:
                self._dirty_bits.add(index)
                self._settled_bits.discard(index)
                if len(self._dirty_bits) == 1:
                    emit_with_batch(self._emit_to_sinks, [(MessageType.DIRTY,)])
                continue
            if t is MessageType.DATA or t is MessageType.RESOLVED:
                if index not in self._dirty_bits:
                    self._dirty_bits.add(index)
                    emit_with_batch(self._emit_to_sinks, [(MessageType.DIRTY,)])
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
                if len(self._complete_bits) >= len(self._deps) > 0:
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
