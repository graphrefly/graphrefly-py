"""``dynamic_node`` — runtime dep tracking with diamond resolution.

Unlike ``node()`` where deps are fixed at construction, ``dynamic_node``
discovers deps at runtime via a tracking ``get()`` proxy. After each
recompute, deps are diffed: new deps are connected, removed deps are
disconnected, and bitmasks are rebuilt. Kept deps retain their
subscriptions (no teardown/reconnect churn).

``DynamicNodeImpl`` extends :class:`~graphrefly.core.node_base.NodeBase`
which provides subscribe / sink / lifecycle machinery. This file only adds:

- Dynamic dep tracking (_deps, _dep_unsubs, _dep_index_map)
- Diamond resolution via set-based dirty/settled/complete bits
- ``_run_fn`` with ``get()`` proxy for dep discovery
- ``_rewire`` for hot-swapping deps without full teardown
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from graphrefly.core.node_base import _SENTINEL, NodeBase
from graphrefly.core.protocol import Messages, MessageType, down_with_batch

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
        equals=equals,
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
# Implementation
# ---------------------------------------------------------------------------


class DynamicNodeImpl[T](NodeBase[T]):
    """Internal implementation of ``dynamic_node``.

    Extends :class:`NodeBase` for shared subscribe/sink/lifecycle machinery.
    Only adds dynamic dep tracking, diamond resolution, and rewiring.
    """

    __slots__ = (
        "_auto_complete",
        "_complete_bits",
        "_dep_index_map",
        "_dep_unsubs",
        "_deps",
        "_dirty_bits",
        "_fn",
        "_rewiring",
        "_settled_bits",
    )

    def __init__(
        self,
        fn: DynamicNodeFn[T],
        *,
        name: str | None,
        equals: Callable[[Any, Any], bool] | None,
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
        super().__init__(
            name=name,
            describe_kind=describe_kind,
            equals=equals,
            initial=_SENTINEL,
            meta=meta,
            guard=guard,
            on_message=on_message,
            on_resubscribe=on_resubscribe,
            resubscribable=resubscribable,
            reset_on_teardown=reset_on_teardown,
            thread_safe=thread_safe,
        )

        self._fn = fn
        self._auto_complete = complete_when_deps_complete
        self._rewiring = False

        # Dynamic deps tracking
        self._deps: list[Any] = []
        self._dep_unsubs: list[Callable[[], None]] = []
        self._dep_index_map: dict[int, int] = {}  # id(dep) -> index
        self._dirty_bits: set[int] = set()
        self._settled_bits: set[int] = set()
        self._complete_bits: set[int] = set()

    # --- NodeBase abstract overrides ---

    @property
    def v(self) -> None:
        """Versioning not yet supported on DynamicNodeImpl."""
        return None

    def _up_internal(self, messages: Messages) -> None:
        """Send messages upstream to currently-tracked deps (guard-free)."""
        for dep in self._deps:
            u = getattr(dep, "up", None)
            if u is not None:
                u(messages, internal=True)

    def _on_activate(self) -> None:
        """First subscriber — connect to deps and run."""
        self._connect()

    def _do_deactivate(self) -> None:
        """Last subscriber gone — disconnect from all deps."""
        self._disconnect()

    def _on_invalidate(self) -> None:
        """No cleanup needed for dynamic nodes on INVALIDATE."""

    def _on_teardown(self) -> None:
        """No cleanup needed for dynamic nodes on TEARDOWN."""

    # --- Public transport ---

    def up(
        self,
        messages: Messages,
        *,
        actor: Any = None,
        internal: bool = False,
        guard_action: str = "write",
        **_kw: Any,
    ) -> None:
        """Send messages upstream to currently-tracked deps."""
        if not self._deps:
            return
        if not internal and self._guard is not None:
            self._guard_and_record(actor, guard_action)
        for dep in self._deps:
            u = getattr(dep, "up", None)
            if u is not None:
                u(messages, internal=internal)

    def unsubscribe(self) -> None:
        """Disconnect from all upstream deps."""
        self._disconnect()

    # --- Dynamic dep management ---

    def _connect(self) -> None:
        if self._dep_unsubs:
            return
        self._status = "settled"
        self._dirty_bits.clear()
        self._settled_bits.clear()
        self._complete_bits.clear()
        self._run_fn()

    def _disconnect(self) -> None:
        if not self._dep_unsubs:
            return
        for unsub in self._dep_unsubs:
            unsub()
        self._dep_unsubs = []
        self._deps = []
        self._dep_index_map.clear()
        self._dirty_bits.clear()
        self._settled_bits.clear()
        self._complete_bits.clear()
        # ROM/RAM rule (GRAPHREFLY-SPEC §2.2): dynamic nodes are always
        # compute nodes — clear cache on disconnect.
        self._cached = _SENTINEL
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

            # START from deps is informational — silently consumed.
            if t is MessageType.START:
                continue
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
