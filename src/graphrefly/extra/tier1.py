"""Tier 1 sync operators — built from :func:`~graphrefly.core.node.node` (roadmap 2.1)."""

from __future__ import annotations

import operator as op
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from graphrefly.core.node import Node, NodeActions, node
from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import PipeOperator, pipe, producer

# --- unary transforms ---------------------------------------------------------


def map(
    fn: Callable[[Any], Any],
    *,
    equals: Callable[[Any, Any], bool] | None = None,
) -> PipeOperator:
    """Map each upstream value through ``fn``."""

    def _op(src: Node[Any]) -> Node[Any]:
        opts: dict[str, Any] = {"describe_kind": "map"}
        if equals is not None:
            opts["equals"] = equals
        return node([src], lambda deps, _: fn(deps[0]), **opts)

    return _op


def filter(
    predicate: Callable[[Any], bool],
) -> PipeOperator:
    """Forward values where ``predicate`` is true; otherwise emit ``RESOLVED``.

    Pure predicate gate — no implicit dedup (use ``distinct_until_changed`` for that).
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def compute(deps: list[Any], actions: NodeActions) -> Any:
            v = deps[0]
            if not predicate(v):
                actions.down([(MessageType.RESOLVED,)])
                return None
            return v

        return node([src], compute, describe_kind="filter")

    return _op


def scan(
    reducer: Callable[[Any, Any], Any],
    seed: Any,
    *,
    equals: Callable[[Any, Any], bool] | None = None,
) -> PipeOperator:
    """Fold upstream values with ``reducer(acc, value) -> acc``; emit after each step."""

    def _op(src: Node[Any]) -> Node[Any]:
        acc = [seed]

        def compute(deps: list[Any], _actions: NodeActions) -> Any:
            v = deps[0]
            acc[0] = reducer(acc[0], v)
            return acc[0]

        eq = equals if equals is not None else op.is_
        return node(
            [src],
            compute,
            describe_kind="scan",
            initial=seed,
            equals=eq,
            reset_on_teardown=True,
        )

    return _op


def reduce(  # noqa: A001 — roadmap API name
    reducer: Callable[[Any, Any], Any],
    seed: Any,
) -> PipeOperator:
    """Reduce to a single value emitted when ``source`` completes.

    On an empty completion (no prior ``DATA``), emits ``seed``.
    """

    def _op(src: Node[Any]) -> Node[Any]:
        acc = [seed]
        saw_data = [False]

        def compute(deps: list[Any], _actions: NodeActions) -> Any:
            saw_data[0] = True
            acc[0] = reducer(acc[0], deps[0])
            return None

        def on_message(msg: Any, _index: int, actions: NodeActions) -> bool:
            if msg[0] is MessageType.COMPLETE:
                if not saw_data[0]:
                    acc[0] = seed
                actions.emit(acc[0])
                actions.down([(MessageType.COMPLETE,)])
                return True
            return False

        return node(
            [src],
            compute,
            on_message=on_message,
            describe_kind="reduce",
            complete_when_deps_complete=False,
        )

    return _op


def take(n: int) -> PipeOperator:
    """Emit at most ``n`` wire ``DATA`` values, then ``COMPLETE`` (not connect pull)."""

    def _op(src: Node[Any]) -> Node[Any]:
        count = [0]
        holder: list[Node[Any] | None] = [None]

        def compute(_deps: list[Any], actions: NodeActions) -> Any:
            if n <= 0:
                actions.down([(MessageType.COMPLETE,)])
                u = holder[0]
                if u is not None:
                    u.unsubscribe()
            return None

        def on_message(msg: Any, _index: int, actions: NodeActions) -> bool:
            if n <= 0:
                if msg[0] is MessageType.ERROR:
                    actions.down([msg])
                return True
            t = msg[0]
            if t is MessageType.DIRTY:
                actions.down([(MessageType.DIRTY,)])
                return True
            if t is MessageType.RESOLVED:
                actions.down([(MessageType.RESOLVED,)])
                return True
            if t is MessageType.DATA:
                count[0] += 1
                actions.emit(msg[1])
                if count[0] >= n:
                    actions.down([(MessageType.COMPLETE,)])
                    u = holder[0]
                    if u is not None:
                        u.unsubscribe()
                return True
            return False

        out = node(
            [src],
            compute,
            on_message=on_message,
            describe_kind="take",
            complete_when_deps_complete=False,
        )
        holder[0] = out
        return out

    return _op


def skip(n: int) -> PipeOperator:
    """Drop the first ``n`` wire ``DATA`` payloads (not connect-time pull)."""

    def _op(src: Node[Any]) -> Node[Any]:
        count = [0]
        last: list[Any] = [None]

        def compute(_deps: list[Any], _actions: NodeActions) -> Any:
            return last[0]

        def on_message(msg: Any, _index: int, actions: NodeActions) -> bool:
            t = msg[0]
            if t is MessageType.DIRTY:
                actions.down([(MessageType.DIRTY,)])
                return True
            if t is MessageType.RESOLVED:
                actions.down([(MessageType.RESOLVED,)])
                return True
            if t is MessageType.DATA:
                count[0] += 1
                if count[0] <= n:
                    actions.down([(MessageType.RESOLVED,)])
                else:
                    last[0] = msg[1]
                    actions.emit(msg[1])
                return True
            return False

        return node(
            [src],
            compute,
            on_message=on_message,
            describe_kind="skip",
        )

    return _op


def take_while(predicate: Callable[[Any], bool]) -> PipeOperator:
    """Emit while ``predicate`` holds; on first false, ``COMPLETE``.

    Predicate exceptions propagate via node-level error handling (spec §2.4).
    """

    def _op(src: Node[Any]) -> Node[Any]:
        done = [False]

        def compute(deps: list[Any], actions: NodeActions) -> Any:
            if done[0]:
                return None
            v = deps[0]
            if not predicate(v):
                done[0] = True
                actions.down([(MessageType.COMPLETE,)])
                return None
            return v

        return node(
            [src],
            compute,
            describe_kind="take_while",
            complete_when_deps_complete=False,
        )

    return _op


def take_until(
    notifier: Node[Any],
    *,
    predicate: Callable[[Any], bool] | None = None,
) -> PipeOperator:
    """Forward ``src`` until ``notifier`` delivers a matching message, then ``COMPLETE``.

    By default triggers on ``DATA`` from the notifier. Pass ``predicate`` for custom
    trigger logic (receives the full message tuple).
    """
    pred = predicate if predicate is not None else (lambda msg: msg[0] is MessageType.DATA)

    def _op(src: Node[Any]) -> Node[Any]:
        fired = [False]

        def compute(deps: list[Any], _actions: NodeActions) -> Any:
            return deps[0]

        def on_message(msg: Any, index: int, actions: NodeActions) -> bool:
            if fired[0]:
                return True
            if index == 1:
                if pred(msg):
                    fired[0] = True
                    actions.down([(MessageType.COMPLETE,)])
                    return True
                return True
            return False

        return node(
            [src, notifier],
            compute,
            on_message=on_message,
            describe_kind="take_until",
            complete_when_deps_complete=False,
        )

    return _op


def first() -> PipeOperator:
    """Emit the first ``DATA`` then ``COMPLETE``."""
    return take(1)


def find(predicate: Callable[[Any], bool]) -> PipeOperator:
    """First value satisfying ``predicate``, then ``COMPLETE``."""
    return lambda src: pipe(src, filter(predicate), take(1))


def element_at(index: int) -> PipeOperator:
    """Emit the value at zero-based emission index ``index`` then ``COMPLETE``."""
    return lambda src: pipe(src, skip(index), take(1))


_LAST_NO_DEFAULT = object()


def last(*, default: Any = _LAST_NO_DEFAULT) -> PipeOperator:
    """Buffer ``DATA``; on ``COMPLETE``, emit the final value (or ``default``).

    If no ``default`` is given and the source completes without emitting, only ``COMPLETE``
    is forwarded (no DATA).
    """
    use_default = default is not _LAST_NO_DEFAULT

    def _op(src: Node[Any]) -> Node[Any]:
        buf: list[Any] = [None]
        has_data = [False]

        def compute(_deps: list[Any], _actions: NodeActions) -> Any:
            return None

        def on_message(msg: Any, _index: int, actions: NodeActions) -> bool:
            t = msg[0]
            if t is MessageType.DATA:
                buf[0] = msg[1]
                has_data[0] = True
                return True
            if t is MessageType.COMPLETE:
                if has_data[0]:
                    actions.emit(buf[0])
                elif use_default:
                    actions.emit(default)
                actions.down([(MessageType.COMPLETE,)])
                return True
            return False

        init = default if use_default else None
        return node(
            [src],
            compute,
            on_message=on_message,
            describe_kind="last",
            initial=init,
            complete_when_deps_complete=False,
        )

    return _op


def start_with(value: Any) -> PipeOperator:
    """Emit ``value`` first, then every value from ``src``."""

    def _op(src: Node[Any]) -> Node[Any]:
        prepended = [False]

        def compute(deps: list[Any], actions: NodeActions) -> Any:
            if not prepended[0]:
                prepended[0] = True
                actions.emit(value)
            actions.emit(deps[0])
            return None

        return node([src], compute, describe_kind="start_with")

    return _op


def tap(side_effect: Callable[[Any], None]) -> PipeOperator:
    """Invoke ``side_effect(value)`` for each emission; value unchanged."""

    def _op(src: Node[Any]) -> Node[Any]:
        def compute(deps: list[Any], _actions: NodeActions) -> Any:
            v = deps[0]
            side_effect(v)
            return v

        return node([src], compute, describe_kind="tap")

    return _op


def distinct_until_changed(
    equals: Callable[[Any, Any], bool] | None = None,
) -> PipeOperator:
    """Suppress consecutive duplicates (default: ``is``)."""
    eq = equals if equals is not None else op.is_

    def _op(src: Node[Any]) -> Node[Any]:
        return node([src], lambda d, _: d[0], equals=eq, describe_kind="distinct_until_changed")

    return _op


def pairwise() -> PipeOperator:
    """Emit ``(previous, current)`` pairs; first emission waits for two values."""

    def _op(src: Node[Any]) -> Node[Any]:
        prev: list[Any] = [object()]
        p0 = prev[0]

        def compute(deps: list[Any], actions: NodeActions) -> Any:
            v = deps[0]
            if prev[0] is p0:
                prev[0] = v
                actions.down([(MessageType.RESOLVED,)])
                return None
            pair = (prev[0], v)
            prev[0] = v
            return pair

        return node([src], compute, describe_kind="pairwise")

    return _op


# --- multi-source -------------------------------------------------------------


def combine(*sources: Node[Any]) -> Node[Any]:
    """Tuple of all dependency values (``derived``)."""
    srcs = list(sources)
    if not srcs:
        return node([], lambda _d, _a: (), describe_kind="combine")
    return node(srcs, lambda deps, _: tuple(deps), describe_kind="combine")


def with_latest_from(other: Node[Any]) -> PipeOperator:
    """When primary settles, emit ``(primary, latest_secondary)``.

    Updates from ``other`` alone refresh the cached secondary value but do not emit.
    """

    def _op(main: Node[Any]) -> Node[Any]:
        latest_b: list[Any] = [None]
        has_b = [False]

        def on_message(msg: Any, i: int, actions: NodeActions) -> bool:
            t = msg[0]
            if i == 1 and t in (MessageType.DATA, MessageType.RESOLVED):
                latest_b[0] = other.get()
                has_b[0] = True
                return True
            if i == 0 and t in (MessageType.DATA, MessageType.RESOLVED):
                if not has_b[0]:
                    latest_b[0] = other.get()
                    has_b[0] = True
                actions.emit((main.get(), latest_b[0]))
                return True
            if i == 0 and t is MessageType.DIRTY:
                actions.down([(MessageType.DIRTY,)])
                return True
            if i == 1 and t is MessageType.DIRTY:
                return True
            if t in (MessageType.COMPLETE, MessageType.ERROR):
                actions.down([msg])
                return True
            actions.down([msg])
            return True

        return node(
            [main, other],
            lambda _d, _a: None,
            on_message=on_message,
            describe_kind="with_latest_from",
        )

    return _op


def merge(*sources: Node[Any]) -> Node[Any]:
    """Last ``DATA`` from any dependency wins; ``COMPLETE`` when all complete."""
    srcs = list(sources)
    if not srcs:
        def _empty_fn(_d: list[Any], a: NodeActions) -> None:
            a.down([(MessageType.COMPLETE,)])

        return producer(_empty_fn)
    if len(srcs) == 1:
        return srcs[0]

    n = len(srcs)
    dirty_mask = [0]
    latest: list[Any] = [None]
    active = [n]
    any_data = [False]

    def compute(_deps: list[Any], _actions: NodeActions) -> Any:
        return latest[0]

    def on_message(msg: Any, index: int, actions: NodeActions) -> bool:
        t = msg[0]
        dm = dirty_mask[0]
        if t is MessageType.DIRTY:
            was_clean = dm == 0
            dirty_mask[0] = dm | (1 << index)
            if was_clean:
                any_data[0] = False
                actions.down([(MessageType.DIRTY,)])
            return True
        if t is MessageType.RESOLVED:
            if dm & (1 << index):
                dirty_mask[0] = dm & ~(1 << index)
                if dirty_mask[0] == 0 and not any_data[0]:
                    actions.down([(MessageType.RESOLVED,)])
            return True
        if t is MessageType.DATA:
            dirty_mask[0] = dirty_mask[0] & ~(1 << index)
            any_data[0] = True
            latest[0] = msg[1]
            actions.emit(msg[1])
            return True
        if t is MessageType.COMPLETE:
            dirty_mask[0] = dirty_mask[0] & ~(1 << index)
            active[0] -= 1
            if active[0] == 0:
                actions.down([(MessageType.COMPLETE,)])
            return True
        if t is MessageType.ERROR:
            actions.down([msg])
            return True
        return False

    return node(
        srcs,
        compute,
        on_message=on_message,
        describe_kind="merge",
        complete_when_deps_complete=False,
    )


def zip(  # noqa: A001
    *sources: Node[Any],
    max_buffer: int = 0,
) -> Node[Any]:
    """Zip emissions by index; optional ``max_buffer`` drops oldest when > 0."""
    srcs = list(sources)
    if not srcs:
        return node([], lambda _d, _a: (), describe_kind="zip")
    if len(srcs) == 1:
        return node(srcs, lambda d, _: (d[0],), describe_kind="zip")

    n = len(srcs)
    buffers = [deque[Any]() for _ in range(n)]
    dirty_mask = [0]
    active = [n]
    any_data = [False]

    def try_emit(actions: NodeActions) -> None:
        while all(len(b) > 0 for b in buffers):
            tup = tuple(b.popleft() for b in buffers)
            actions.emit(tup)

    def compute(_deps: list[Any], _actions: NodeActions) -> Any:
        return None

    def on_message(msg: Any, index: int, actions: NodeActions) -> bool:
        t = msg[0]
        dm = dirty_mask[0]
        if t is MessageType.DIRTY:
            was_clean = dm == 0
            dirty_mask[0] = dm | (1 << index)
            if was_clean:
                any_data[0] = False
                actions.down([(MessageType.DIRTY,)])
            return True
        if t is MessageType.RESOLVED:
            if dm & (1 << index):
                dirty_mask[0] = dm & ~(1 << index)
                if dirty_mask[0] == 0:
                    if any_data[0]:
                        try_emit(actions)
                    else:
                        actions.down([(MessageType.RESOLVED,)])
            return True
        if t is MessageType.DATA:
            dirty_mask[0] = dm & ~(1 << index)
            buffers[index].append(msg[1])
            if max_buffer > 0 and len(buffers[index]) > max_buffer:
                buffers[index].popleft()
            any_data[0] = True
            if dirty_mask[0] == 0:
                try_emit(actions)
            return True
        if t is MessageType.COMPLETE:
            active[0] -= 1
            if active[0] == 0 or len(buffers[index]) == 0:
                actions.down([(MessageType.COMPLETE,)])
            return True
        if t is MessageType.ERROR:
            actions.down([msg])
            return True
        return False

    return node(
        srcs,
        compute,
        on_message=on_message,
        describe_kind="zip",
        complete_when_deps_complete=False,
    )


def concat(second: Node[Any]) -> PipeOperator:
    """After ``first`` completes, continue with ``second``.

    While ``first`` is active, ``DATA`` from ``second`` is buffered and replayed
    after the handoff so ``pipe(one_shot, concat(live_src))`` does not drop values.
    """

    def op(first: Node[Any]) -> Node[Any]:
        phase = [0]  # 0 = first, 1 = second
        holder: list[Node[Any] | None] = [None]
        pending_values: deque[Any] = deque()

        def flush_pending(actions: NodeActions) -> None:
            while pending_values:
                actions.emit(pending_values.popleft())

        def compute(deps: list[Any], _actions: NodeActions) -> Any:
            return None

        def on_message(msg: Any, index: int, actions: NodeActions) -> bool:
            t = msg[0]
            if phase[0] == 0 and index == 1:
                if t is MessageType.DATA:
                    pending_values.append(msg[1])
                elif t is MessageType.ERROR:
                    actions.down([msg])
                return True
            if phase[0] == 1 and index == 0:
                return True
            if t is MessageType.COMPLETE and phase[0] == 0 and index == 0:
                phase[0] = 1
                flush_pending(actions)
                return True
            if t is MessageType.DIRTY:
                actions.down([(MessageType.DIRTY,)])
                return True
            if t is MessageType.RESOLVED:
                actions.down([(MessageType.RESOLVED,)])
                return True
            if t is MessageType.DATA:
                actions.emit(msg[1])
                return True
            if t is MessageType.COMPLETE:
                actions.down([(MessageType.COMPLETE,)])
                u = holder[0]
                if u is not None:
                    u.unsubscribe()
                return True
            if t is MessageType.ERROR:
                actions.down([msg])
                return True
            return False

        out = node(
            [first, second],
            compute,
            on_message=on_message,
            describe_kind="concat",
            complete_when_deps_complete=False,
        )
        holder[0] = out
        return out

    return op


def race(*sources: Node[Any]) -> Node[Any]:
    """First dependency to emit ``DATA`` wins; later only that source is forwarded."""
    srcs = list(sources)
    if not srcs:
        def _empty_race(_d: list[Any], a: NodeActions) -> None:
            a.down([(MessageType.COMPLETE,)])

        return producer(_empty_race)
    if len(srcs) == 1:
        return srcs[0]

    winner: list[int | None] = [None]

    def compute(_deps: list[Any], _actions: NodeActions) -> Any:
        return None

    def on_message(msg: Any, index: int, actions: NodeActions) -> bool:
        t = msg[0]
        w = winner[0]
        if w is not None and index != w:
            return True
        if t is MessageType.DATA and w is None:
            winner[0] = index
            actions.emit(msg[1])
            return True
        if w is not None and index == w:
            if t is MessageType.DATA:
                actions.emit(msg[1])
                return True
            if t is MessageType.DIRTY:
                actions.down([(MessageType.DIRTY,)])
                return True
            if t is MessageType.RESOLVED:
                actions.down([(MessageType.RESOLVED,)])
                return True
            if t in (MessageType.COMPLETE, MessageType.ERROR):
                actions.down([msg])
                return True
            return True
        if w is None:
            if t is MessageType.DIRTY:
                actions.down([(MessageType.DIRTY,)])
                return True
            if t is MessageType.RESOLVED:
                actions.down([(MessageType.RESOLVED,)])
                return True
            if t is MessageType.COMPLETE:
                actions.down([(MessageType.COMPLETE,)])
                return True
            if t is MessageType.ERROR:
                actions.down([msg])
                return True
        return False

    return node(
        srcs,
        compute,
        on_message=on_message,
        describe_kind="race",
        complete_when_deps_complete=False,
    )


__all__ = [
    "combine",
    "concat",
    "distinct_until_changed",
    "element_at",
    "filter",
    "find",
    "first",
    "last",
    "map",
    "merge",
    "pairwise",
    "race",
    "reduce",
    "scan",
    "skip",
    "start_with",
    "take",
    "take_until",
    "take_while",
    "tap",
    "with_latest_from",
    "zip",
]
