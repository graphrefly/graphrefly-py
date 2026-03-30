"""Tier 1 sync operators built from :func:`~graphrefly.core.node.node` (roadmap §2.1).

Most callables return a :class:`~graphrefly.core.sugar.PipeOperator` for use with
:func:`~graphrefly.core.sugar.pipe`. ``combine``, ``merge``, ``zip``, and ``race`` return a
:class:`~graphrefly.core.node.Node` directly.

Use Google-style docstrings here (``docs/docs-guidance.md``); they feed the site via
``website/scripts/gen_api_docs.py``.
"""

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
    """Map each upstream settled value through ``fn``.

    Args:
        fn: Transform applied to each dependency value.
        equals: Optional equality for ``RESOLVED`` detection on the inner node.

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator` wrapping the upstream node.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import map as grf_map
        >>> n = pipe(state(1), grf_map(lambda x: x * 2))
    """

    def _op(src: Node[Any]) -> Node[Any]:
        opts: dict[str, Any] = {"describe_kind": "map"}
        if equals is not None:
            opts["equals"] = equals
        return node([src], lambda deps, _: fn(deps[0]), **opts)

    return _op


def filter(
    predicate: Callable[[Any], bool],
) -> PipeOperator:
    """Forward values where ``predicate`` is true; otherwise emit ``RESOLVED`` (no ``DATA``).

    Pure predicate gate — no implicit dedup (use ``distinct_until_changed`` for that).

    Args:
        predicate: Inclusion test for each value.

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import filter as grf_filter
        >>> n = pipe(state(1), grf_filter(lambda x: x > 0))
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
    """Fold upstream values with ``reducer(acc, value) -> acc``; emit accumulator after each step.

    Unlike RxJS, seed is always required — there is no seedless mode where the first value
    silently becomes the accumulator.

    Args:
        reducer: Accumulator update.
        seed: Initial accumulator (also used for ``initial`` on the inner node).
        equals: Optional equality for consecutive emissions (default ``operator.eq``).

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import scan as grf_scan
        >>> n = pipe(state(1), grf_scan(lambda a, x: a + x, 0))
    """

    def _op(src: Node[Any]) -> Node[Any]:
        acc = [seed]

        def compute(deps: list[Any], _actions: NodeActions) -> Any:
            v = deps[0]
            acc[0] = reducer(acc[0], v)
            return acc[0]

        eq = equals if equals is not None else op.eq
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
    """Reduce to one value emitted when the source completes.

    Unlike RxJS, seed is always required. If the source completes without emitting DATA,
    the seed value is emitted (RxJS would throw without a seed).

    On an empty completion (no prior ``DATA``), emits ``seed``.

    Args:
        reducer: Accumulator update (return value is not used until completion).
        seed: Value used when the source completes with no prior ``DATA``.

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import reduce as grf_reduce
        >>> n = pipe(state(1), grf_reduce(lambda a, x: a + x, 0))
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
    """Emit at most ``n`` wire ``DATA`` values, then ``COMPLETE`` (``RESOLVED`` does not count).

    Args:
        n: Maximum ``DATA`` emissions; ``n <= 0`` completes immediately.

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import take as grf_take
        >>> n = pipe(state(0), grf_take(3))
    """

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
    """Drop the first ``n`` wire ``DATA`` payloads (``RESOLVED`` does not advance the counter).

    Args:
        n: Number of ``DATA`` values to suppress before forwarding.

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import skip as grf_skip
        >>> n = pipe(state(0), grf_skip(2))
    """

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

    Args:
        predicate: Continuation test for each value.

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import take_while as grf_tw
        >>> n = pipe(state(1), grf_tw(lambda x: x < 10))
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
    """Forward the main source until ``notifier`` matches ``predicate``, then ``COMPLETE``.

    Default ``predicate`` fires on ``DATA`` from the notifier (full message tuple is passed in).

    Args:
        notifier: Second input observed for the stop condition.
        predicate: Optional ``(msg) -> bool``; default tests ``msg[0] is MessageType.DATA``.

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, producer, state
        >>> from graphrefly.extra import take_until as grf_tu
        >>> stop = producer(lambda _d, a: a.emit(0))
        >>> n = pipe(state(1), grf_tu(stop))
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
    """Emit the first ``DATA`` then ``COMPLETE`` (same as ``take(1)``).

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import first as grf_first
        >>> n = pipe(state(42), grf_first())
    """
    return take(1)


def find(predicate: Callable[[Any], bool]) -> PipeOperator:
    """Emit the first value satisfying ``predicate``, then ``COMPLETE``.

    Args:
        predicate: Match test.

    Returns:
        A unary callable ``(Node) -> Node`` composed from ``filter`` and ``take(1)``.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import find as grf_find
        >>> n = pipe(state(1), grf_find(lambda x: x > 0))
    """
    return lambda src: pipe(src, filter(predicate), take(1))


def element_at(index: int) -> PipeOperator:
    """Emit the value at zero-based ``DATA`` index ``index``, then ``COMPLETE``.

    Args:
        index: Number of prior ``DATA`` emissions to skip.

    Returns:
        A unary callable ``(Node) -> Node`` composed from ``skip`` and ``take(1)``.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import element_at as grf_at
        >>> n = pipe(state(0), grf_at(2))
    """
    return lambda src: pipe(src, skip(index), take(1))


_LAST_NO_DEFAULT = object()


def last(*, default: Any = _LAST_NO_DEFAULT) -> PipeOperator:
    """Buffer ``DATA``; on ``COMPLETE``, emit the final value (or ``default``).

    If no ``default`` is given and the source completes without emitting, only ``COMPLETE``
    is forwarded (no ``DATA``).

    Args:
        default: Optional value emitted when the source completes empty.

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import last as grf_last
        >>> n = pipe(state(1), grf_last(default=0))
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
    """Emit ``value`` as ``DATA`` first, then forward every value from the source.

    Args:
        value: Prepended emission before upstream values.

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import start_with as grf_sw
        >>> n = pipe(state(2), grf_sw(0))
    """

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


def tap(fn_or_observer: Callable[[Any], None] | dict[str, Callable[..., None]]) -> PipeOperator:
    """Invoke side effects for each emission; value passes through unchanged.

    When ``fn_or_observer`` is a callable, it is invoked for each ``DATA`` value (classic mode).

    When ``fn_or_observer`` is a dict, it may contain keys ``data``, ``error``, and ``complete``,
    each a callable invoked for the corresponding message type:

    - ``data(value)`` — called on each ``DATA``
    - ``error(err)`` — called on ``ERROR``
    - ``complete()`` — called on ``COMPLETE``

    Args:
        fn_or_observer: A callable ``(value) -> None`` or an observer dict.

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import tap as grf_tap
        >>> n = pipe(state(1), grf_tap(lambda x: None))
        >>> n2 = pipe(state(1), grf_tap({"data": lambda x: None, "complete": lambda: None}))
    """

    if callable(fn_or_observer):
        side_effect = fn_or_observer

        def _op_fn(src: Node[Any]) -> Node[Any]:
            def compute(deps: list[Any], _actions: NodeActions) -> Any:
                v = deps[0]
                side_effect(v)
                return v

            return node([src], compute, describe_kind="tap")

        return _op_fn

    # Observer dict mode
    obs = fn_or_observer
    on_data: Callable[[Any], None] | None = obs.get("data")
    on_error: Callable[[Any], None] | None = obs.get("error")
    on_complete: Callable[[], None] | None = obs.get("complete")

    def _op_obs(src: Node[Any]) -> Node[Any]:
        def compute(deps: list[Any], _actions: NodeActions) -> Any:
            v = deps[0]
            if on_data is not None:
                on_data(v)
            return v

        def on_message(msg: Any, _index: int, actions: NodeActions) -> bool:
            t = msg[0]
            if t is MessageType.ERROR:
                if on_error is not None:
                    on_error(msg[1] if len(msg) > 1 else None)
                actions.down([msg])
                return True
            if t is MessageType.COMPLETE:
                if on_complete is not None:
                    on_complete()
                actions.down([(MessageType.COMPLETE,)])
                return True
            return False

        return node([src], compute, on_message=on_message, describe_kind="tap")

    return _op_obs


def distinct_until_changed(
    equals: Callable[[Any, Any], bool] | None = None,
) -> PipeOperator:
    """Suppress consecutive duplicates using ``equals`` (default: ``operator.eq``).

    Args:
        equals: Optional binary equality for adjacent values.

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import distinct_until_changed as grf_duc
        >>> n = pipe(state(1), grf_duc())
    """
    eq = equals if equals is not None else op.eq

    def _op(src: Node[Any]) -> Node[Any]:
        return node([src], lambda d, _: d[0], equals=eq, describe_kind="distinct_until_changed")

    return _op


def pairwise() -> PipeOperator:
    """Emit ``(previous, current)`` pairs; the first upstream value yields ``RESOLVED`` only.

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import pairwise as grf_pw
        >>> n = pipe(state(0), grf_pw())
    """

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
    """Combine latest values from all sources into a tuple whenever any settles.

    Args:
        *sources: Upstream nodes (empty → empty tuple node).

    Returns:
        A :class:`~graphrefly.core.node.Node` emitting ``tuple`` of dependency values.

    Examples:
        >>> from graphrefly.extra import combine
        >>> from graphrefly import state
        >>> n = combine(state(1), state("a"))
    """
    srcs = list(sources)
    if not srcs:
        return node([], lambda _d, _a: (), describe_kind="combine")
    return node(srcs, lambda deps, _: tuple(deps), describe_kind="combine")


def with_latest_from(other: Node[Any]) -> PipeOperator:
    """When the primary source settles, emit ``(primary, latest_secondary)``.

    Updates from ``other`` alone refresh the cached secondary value but do not emit.

    Args:
        other: Secondary node whose latest value is paired on primary emissions.

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import with_latest_from as grf_wlf
        >>> n = pipe(state(1), grf_wlf(state("x")))
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
    """Merge ``DATA`` from any dependency; ``COMPLETE`` only after every source completes.

    Args:
        *sources: Upstreams to merge (empty → immediate ``COMPLETE`` node).

    Returns:
        A :class:`~graphrefly.core.node.Node`.

    Examples:
        >>> from graphrefly.extra import merge
        >>> from graphrefly import state
        >>> n = merge(state(1), state(2))
    """
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
    """Zip one ``DATA`` from each source per cycle into a tuple.

    Args:
        *sources: Upstreams to zip.
        max_buffer: When ``> 0``, drop oldest queued values per source beyond this depth.

    Returns:
        A :class:`~graphrefly.core.node.Node` emitting tuples.

    Examples:
        >>> from graphrefly.extra import zip as grf_zip
        >>> from graphrefly import state
        >>> n = grf_zip(state(1), state(2))
    """
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
    """Play ``first`` to completion, then continue with ``second``.

    While ``first`` is active, ``DATA`` from ``second`` is buffered and replayed at handoff.

    Args:
        second: Segment played after the primary completes.

    Returns:
        A :class:`~graphrefly.core.sugar.PipeOperator`.

    Examples:
        >>> from graphrefly import pipe, state
        >>> from graphrefly.extra import concat as grf_cat
        >>> n = pipe(state(1), grf_cat(state(2)))
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
    """First source to emit ``DATA`` wins; subsequent traffic follows only that source.

    Args:
        *sources: Contestants (empty → immediate ``COMPLETE``; one node is returned as-is).

    Returns:
        A :class:`~graphrefly.core.node.Node`.

    Examples:
        >>> from graphrefly.extra import race
        >>> from graphrefly import state
        >>> n = race(state(1), state(2))
    """
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


combine_latest = combine

__all__ = [
    "combine",
    "combine_latest",
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
