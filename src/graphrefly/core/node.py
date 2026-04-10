"""GraphReFly ``node`` primitive — aligned with graphrefly-ts ``src/core/node.ts``.

``NodeImpl`` extends :class:`~graphrefly.core.node_base.NodeBase` which
provides subscribe / sink / lifecycle machinery. This file only adds:

- Dep-wave tracking via pre-set dirty masks
- ``_run_fn`` with identity-skip optimization on ``_last_dep_values``
- Producer start/stop tied to sink count
- ROM/RAM cache semantics: compute nodes clear ``_cached`` on disconnect,
  state sources preserve it (see ``_on_deactivate``).

See GRAPHREFLY-SPEC §§2.1–2.8 and COMPOSITION-GUIDE §§1, 9.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import Any

from graphrefly.core.node_base import _SENTINEL as _SENTINEL
from graphrefly.core.node_base import NO_VALUE as NO_VALUE
from graphrefly.core.node_base import Message as Message
from graphrefly.core.node_base import NodeActions as NodeActions
from graphrefly.core.node_base import NodeBase as NodeBase
from graphrefly.core.node_base import NodeStatus as NodeStatus
from graphrefly.core.node_base import SubscribeHints as SubscribeHints
from graphrefly.core.node_base import (
    _create_bit_set,
    _is_cleanup_fn,
    _is_cleanup_result,
)
from graphrefly.core.node_base import _status_after_message as _status_after_message
from graphrefly.core.node_base import cleanup_result as cleanup_result
from graphrefly.core.protocol import Messages, MessageType
from graphrefly.core.versioning import (
    HashFn,
    NodeVersionInfo,
    VersioningLevel,
    advance_version,
    create_versioning,
    default_hash,
)

type NodeFn = Callable[[list[Any], NodeActions], Any]


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


class NodeImpl[T](NodeBase[T]):
    """Internal implementation — use :func:`node` factory."""

    __slots__ = (
        "_all_deps_complete_mask",
        "_auto_complete",
        "_cleanup",
        "_connecting",
        "_dep_complete_mask",
        "_dep_dirty_mask",
        "_dep_settled_mask",
        "_deps",
        "_fn",
        "_has_deps",
        "_hash_fn",
        "_last_dep_values",
        "_manual_emit_used",
        "_producer_started",
        "_upstream_unsubs",
        "_versioning",
    )

    def __init__(
        self,
        deps: list[NodeImpl[Any]],
        fn: NodeFn | None,
        opts: dict[str, Any],
    ) -> None:
        # Extract options for NodeBase init
        super().__init__(
            name=opts.get("name"),
            describe_kind=opts.get("describe_kind"),
            equals=opts.get("equals"),
            initial=opts.get("initial", _SENTINEL),
            meta=opts.get("meta"),
            guard=opts.get("guard"),
            on_message=opts.get("on_message"),
            on_resubscribe=opts.get("on_resubscribe"),
            resubscribable=bool(opts.get("resubscribable", False)),
            reset_on_teardown=bool(opts.get("reset_on_teardown", False)),
            thread_safe=bool(opts.get("thread_safe", True)),
        )

        self._fn = fn
        self._deps = deps
        self._has_deps = len(deps) > 0
        self._auto_complete: bool = bool(opts.get("complete_when_deps_complete", True))

        # Versioning (GRAPHREFLY-SPEC §7)
        versioning_level: VersioningLevel | None = opts.get("versioning")
        self._hash_fn: HashFn = opts.get("versioning_hash", default_hash)
        self._versioning: NodeVersionInfo | None = (
            create_versioning(
                versioning_level,
                None if self._cached is _SENTINEL else self._cached,
                id=opts.get("versioning_id"),
                hash_fn=self._hash_fn,
            )
            if versioning_level is not None
            else None
        )

        # Compute nodes (fn or deps) start as "disconnected" regardless of
        # initial value — they haven't connected to deps or run fn yet.
        # State nodes (no fn, no deps) keep NodeBase's "settled" when they
        # have an initial value.
        if self._fn is not None or self._has_deps:
            self._status = "disconnected"

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
        self._upstream_unsubs: list[Callable[[], None]] = []

        # Register deps with subgraph locks
        if self._thread_safe:
            from graphrefly.core.subgraph_locks import union_nodes

            for d in self._deps:
                union_nodes(self, d)

    # --- NodeBase overrides ---

    def _manual_down(self, messages: Messages) -> None:
        self._manual_emit_used = True
        self._down_internal(messages)

    def _manual_emit(self, value: Any) -> None:
        self._manual_emit_used = True
        self._down_auto_value(value)

    def _up_internal(self, messages: Messages) -> None:
        if not self._has_deps:
            return
        for dep in self._deps:
            u = getattr(dep, "up", None)
            if u is not None:
                u(messages)

    def _on_activate(self) -> None:
        if self._has_deps:
            self._connect_upstream()
        elif self._fn is not None:
            self._start_producer()

    def _do_deactivate(self) -> None:
        self._disconnect_upstream()
        self._stop_producer()
        # ROM/RAM rule (GRAPHREFLY-SPEC §2.2): compute nodes (anything with
        # fn) clear their cache on disconnect — their value is a function of
        # live subscriptions. State nodes (no fn) preserve _cached.
        if self._fn is not None:
            self._cached = _SENTINEL
            self._last_dep_values = None

    def _on_invalidate(self) -> None:
        if self._cleanup is not None:
            cb = self._cleanup
            self._cleanup = None
            cb()
        self._last_dep_values = None

    def _on_teardown(self) -> None:
        if self._cleanup is not None:
            cb = self._cleanup
            self._cleanup = None
            cb()

    # --- Versioning override ---

    def _handle_local_lifecycle(self, messages: Messages) -> None:
        # Extend base to add versioning tracking on DATA.
        for m in messages:
            t = m[0]
            if t is MessageType.DATA and len(m) >= 2 and self._versioning is not None:
                advance_version(self._versioning, m[1], self._hash_fn)
        super()._handle_local_lifecycle(messages)

    @property
    def v(self) -> NodeVersionInfo | None:
        return self._versioning

    def _apply_versioning(
        self,
        level: VersioningLevel,
        *,
        id: str | None = None,
        hash_fn: HashFn | None = None,
    ) -> None:
        if self._versioning is not None:
            return
        if hash_fn is not None:
            self._hash_fn = hash_fn
        self._versioning = create_versioning(
            level,
            None if self._cached is _SENTINEL else self._cached,
            id=id,
            hash_fn=self._hash_fn,
        )

    # --- Dep wave tracking ---

    def _on_dep_dirty(self, index: int) -> None:
        was_dirty = self._dep_dirty_mask.has(index)
        self._dep_dirty_mask.set(index)
        self._dep_settled_mask.clear(index)
        if not was_dirty:
            self.down([(MessageType.DIRTY,)], internal=True)

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
            self.down([(MessageType.COMPLETE,)], internal=True)

    def _handle_dep_messages(self, index: int, messages: Messages) -> None:
        for msg in messages:
            if self._inspector_hook is not None:
                self._inspector_hook({"kind": "dep_message", "dep_index": index, "message": msg})
            t = msg[0]
            # User-defined message handler gets first look (spec §2.6).
            if self._on_message is not None:
                try:
                    if self._on_message(msg, index, self._actions):
                        continue
                except Exception as err:
                    wrapped = RuntimeError(f'Node "{self._name}": on_message threw: {err}')
                    wrapped.__cause__ = err
                    self.down([(MessageType.ERROR, wrapped)], internal=True)
                    return
            # START from deps is informational — silently consumed.
            if t is MessageType.START:
                continue
            if self._fn is None:
                if t is MessageType.COMPLETE and len(self._deps) > 1:
                    self._dep_complete_mask.set(index)
                    self._maybe_complete_from_deps()
                    continue
                self.down([msg], internal=True)
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
                    self._dep_settled_mask.reset()
                    self._run_fn()
                self._maybe_complete_from_deps()
                continue
            if t is MessageType.ERROR:
                self.down([msg], internal=True)
                continue
            if (
                t is MessageType.INVALIDATE
                or t is MessageType.TEARDOWN
                or t is MessageType.PAUSE
                or t is MessageType.RESUME
            ):
                self.down([msg], internal=True)
                continue
            # Forward unknown message types
            self.down([msg], internal=True)

    def _connect_upstream(self) -> None:
        if not self._has_deps or self._upstream_unsubs:
            return
        # Pre-set dirty mask to all-ones — wave completes when every dep
        # has settled at least once (spec §2.7, first-run gating).
        self._dep_dirty_mask.set_all()
        self._dep_settled_mask.reset()
        self._dep_complete_mask.reset()
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
            cb = self._cleanup
            self._cleanup = None
            cb()
            self._cleanup = None

    def _start_producer(self) -> None:
        if len(self._deps) != 0 or self._fn is None or self._producer_started:
            return
        self._producer_started = True
        self._run_fn()

    def _disconnect_upstream(self) -> None:
        if not self._upstream_unsubs:
            return
        for u in self._upstream_unsubs:
            u()
        self._upstream_unsubs.clear()
        self._dep_dirty_mask.reset()
        self._dep_settled_mask.reset()
        self._dep_complete_mask.reset()
        self._status = "disconnected"

    def _run_fn_body(self) -> None:
        if self._terminal and not self._resubscribable:
            return

        try:
            dep_values = [d.get() for d in self._deps]
            prev = self._last_dep_values
            n = len(dep_values)
            if (
                n > 0
                and prev is not None
                and len(prev) == n
                and all(dep_values[i] is prev[i] for i in range(n))
            ):
                if self._status == "dirty":
                    self.down([(MessageType.RESOLVED,)], internal=True)
                return
            if self._cleanup is not None:
                cb = self._cleanup
                self._cleanup = None
                cb()
            self._manual_emit_used = False
            self._last_dep_values = dep_values
            if self._inspector_hook is not None:
                self._inspector_hook({"kind": "run", "dep_values": dep_values})
            out = self._fn(dep_values, self._actions)  # type: ignore[misc]
            if _is_cleanup_result(out):
                self._cleanup = out["cleanup"]
                if self._manual_emit_used:
                    return
                if "value" in out:
                    self._down_auto_value(out["value"])
                return
            if _is_cleanup_fn(out):
                self._cleanup = out
                return
            if self._manual_emit_used:
                return
            if out is None:
                return
            self._down_auto_value(out)
        except Exception as err:
            wrapped = RuntimeError(f'Node "{self._name}": fn threw: {err}')
            wrapped.__cause__ = err
            self.down([(MessageType.ERROR, wrapped)], internal=True)

    def _run_fn(self) -> None:
        if self._fn is None:
            return
        if self._connecting:
            return
        if self._thread_safe:
            from graphrefly.core.subgraph_locks import acquire_subgraph_write_lock_with_defer

            with acquire_subgraph_write_lock_with_defer(self):
                self._run_fn_body()
        else:
            self._run_fn_body()

    # --- Public interface ---

    def up(
        self,
        messages: Messages,
        *,
        actor: Any = None,
        internal: bool = False,
        guard_action: str = "write",
        **_kw: Any,
    ) -> None:
        if not self._has_deps:
            return
        if not internal and self._guard is not None:
            self._guard_and_record(actor, guard_action)
        for dep in self._deps:
            u = getattr(dep, "up", None)
            if u is not None:
                u(messages)

    def unsubscribe(self) -> None:
        if not self._has_deps:
            return
        if self._thread_safe:
            from graphrefly.core.subgraph_locks import acquire_subgraph_write_lock_with_defer

            with acquire_subgraph_write_lock_with_defer(self):
                self._disconnect_upstream()
        else:
            self._disconnect_upstream()


# Public type alias — matches TS ``Node<T>``.
# Assignment alias (not PEP 695 ``type``) so it can be used as a base class.
Node = NodeImpl


def node(
    deps_or_fn: Sequence[NodeImpl[Any]] | NodeFn | dict[str, Any] | None = None,
    fn_or_opts: NodeFn | dict[str, Any] | None = None,
    opts_arg: dict[str, Any] | None = None,
    **kwargs: Any,
) -> NodeImpl[Any]:
    """Create a reactive node (mirrors graphrefly-ts ``node`` overloads).

    Accepts multiple call signatures::

        node()                           # no-dep, no-fn source
        node(fn)                         # producer (no deps)
        node([dep1, dep2], fn)           # derived / operator
        node([dep1], fn, {"name": ...})  # with options dict
        node(name="x", initial=0)        # kwargs shorthand

    Args:
        deps_or_fn: Either a sequence of upstream :class:`NodeImpl` dependencies,
            a bare compute function (producer), an options dict, or ``None``.
        fn_or_opts: Compute function (when ``deps_or_fn`` is a dep list) or an
            options dict.
        opts_arg: Additional options dict when both deps and fn are provided
            positionally.
        **kwargs: Any option key accepted by :class:`NodeImpl` (e.g. ``name``,
            ``initial``, ``equals``, ``meta``, etc.).

    Returns:
        A :class:`NodeImpl` instance.

    Examples:
        >>> from graphrefly import node
        >>> x = node(initial=42, name="x")
        >>> x.get()
        42
    """
    deps: list[NodeImpl[Any]] = []
    fn: NodeFn | None = None
    opts: dict[str, Any] = {}

    if deps_or_fn is None:
        pass
    elif _is_node_sequence(deps_or_fn):
        deps = list(deps_or_fn)  # type: ignore[arg-type]
    elif callable(deps_or_fn):
        fn = deps_or_fn
    elif _is_node_options(deps_or_fn):
        opts = _as_options_dict(deps_or_fn)
    else:
        msg = f"node(): unexpected first argument type {type(deps_or_fn)}"
        raise TypeError(msg)

    if fn_or_opts is None:
        pass
    elif callable(fn_or_opts) and not _is_node_options(fn_or_opts):
        fn = fn_or_opts
    elif _is_node_options(fn_or_opts):
        opts = {**opts, **_as_options_dict(fn_or_opts)}
    else:
        msg = f"node(): unexpected second argument type {type(fn_or_opts)}"
        raise TypeError(msg)

    if opts_arg is not None:
        opts = {**opts, **_as_options_dict(opts_arg)}

    if kwargs:
        opts = {**opts, **kwargs}

    return NodeImpl(deps, fn, opts)
