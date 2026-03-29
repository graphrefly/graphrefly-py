"""Tier 2 operators — async/time/dynamic — from :func:`~graphrefly.core.node.node` (roadmap §2.2).

Dynamic ``*_map`` operators subscribe to inner :class:`~graphrefly.core.node.Node` values from
the mapper. Time-based operators use :class:`threading.Timer`; timers are cancelled on
unsubscribe (same lifecycle as a no-deps ``node`` producer).
"""

from __future__ import annotations

import threading
from collections import deque
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from graphrefly.core.node import Node, NodeActions, node
from graphrefly.core.protocol import Messages, MessageType

if TYPE_CHECKING:
    from collections.abc import Callable

    from graphrefly.core.sugar import PipeOperator

_UNSET: Any = object()


def _msg_val(m: tuple[Any, ...]) -> Any:
    """Payload for a ``DATA`` / ``ERROR`` tuple (GraphReFly messages are at least two elements)."""
    assert len(m) >= 2
    return m[1]


# --- dynamic inner subscription (switch / concat / flat / exhaust) ------------


def switch_map(
    fn: Callable[[Any], Node[Any]],
    *,
    initial: Any = _UNSET,
) -> PipeOperator:
    """Map each outer settled value to an inner node; keep only the latest inner subscription.

    On each outer ``DATA``, the previous inner is unsubscribed. Inner ``DATA`` / ``RESOLVED`` /
    ``DIRTY`` are forwarded; inner ``ERROR`` always terminates; inner ``COMPLETE`` completes
    the output only if the outer has already completed.

    Args:
        fn: ``outer_value -> Node`` for the active inner.
        initial: Optional seed for :meth:`~graphrefly.core.node.Node.get` before the first inner
            emission.

    Returns:
        A unary pipe operator ``(Node) -> Node``.
    """

    has_initial = initial is not _UNSET

    def _op(outer: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            inner_unsub: list[Callable[[], None] | None] = [None]
            outer_done = [False]
            current: list[Node[Any] | None] = [None]

            def teardown_inner() -> None:
                if inner_unsub[0] is not None:
                    inner_unsub[0]()
                    inner_unsub[0] = None
                current[0] = None

            def subscribe_inner(inner: Node[Any]) -> None:
                teardown_inner()
                current[0] = inner
                inner_emitted = [False]
                inner_ended = [False]

                def inner_sink(msgs: Messages) -> None:
                    for m in msgs:
                        t = m[0]
                        if t is MessageType.DATA:
                            inner_emitted[0] = True
                            actions.emit(_msg_val(m))
                        elif t is MessageType.RESOLVED:
                            inner_emitted[0] = True
                            actions.down([(MessageType.RESOLVED,)])
                        elif t is MessageType.DIRTY:
                            actions.down([(MessageType.DIRTY,)])
                        elif t is MessageType.ERROR:
                            inner_unsub[0] = None
                            current[0] = None
                            actions.down([m])
                        elif t is MessageType.COMPLETE:
                            inner_ended[0] = True
                            inner_unsub[0] = None
                            current[0] = None
                            if outer_done[0]:
                                actions.down([(MessageType.COMPLETE,)])
                        else:
                            actions.down([m])

                inner_unsub[0] = inner.subscribe(inner_sink)
                if not inner_emitted[0] and not inner_ended[0]:
                    actions.emit(inner.get())
                if inner_ended[0]:
                    inner_unsub[0] = None

            def outer_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.DATA:
                        subscribe_inner(fn(_msg_val(m)))
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        outer_done[0] = True
                        if inner_unsub[0] is None:
                            actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        teardown_inner()
                        actions.down([m])
                    else:
                        actions.down([m])

            outer_unsub = outer.subscribe(outer_sink)

            def cleanup() -> None:
                teardown_inner()
                outer_unsub()

            return cleanup

        opts: dict[str, Any] = {
            "describe_kind": "switch_map",
            "complete_when_deps_complete": False,
        }
        if has_initial:
            opts["initial"] = initial
        return node(start, **opts)

    return _op


def concat_map(
    fn: Callable[[Any], Node[Any]],
    *,
    initial: Any = _UNSET,
    max_buffer: int = 0,
) -> PipeOperator:
    """Map outer values to inner nodes; run inners strictly one after another.

    While an inner is active, outer ``DATA`` values are queued. ``max_buffer > 0`` drops the
    oldest queued value when the queue would exceed that length.

    Args:
        fn: ``outer_value -> Node``.
        initial: Optional initial ``get()`` value.
        max_buffer: Maximum queued outer keys (``0`` = unlimited).

    Returns:
        A unary pipe operator.
    """

    has_initial = initial is not _UNSET

    def _op(outer: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            inner_unsub: list[Callable[[], None] | None] = [None]
            inner_active = [False]
            outer_done = [False]
            queue: deque[Any] = deque()

            def teardown_inner() -> None:
                if inner_unsub[0] is not None:
                    inner_unsub[0]()
                    inner_unsub[0] = None

            def process_next() -> None:
                if len(queue) == 0:
                    inner_active[0] = False
                    if outer_done[0]:
                        actions.down([(MessageType.COMPLETE,)])
                    return
                subscribe_inner(fn(queue.popleft()))

            def subscribe_inner(inner: Node[Any]) -> None:
                teardown_inner()
                inner_active[0] = True
                inner_emitted = [False]
                inner_ended = [False]

                def inner_sink(msgs: Messages) -> None:
                    for m in msgs:
                        t = m[0]
                        if t is MessageType.DATA:
                            inner_emitted[0] = True
                            actions.emit(_msg_val(m))
                        elif t is MessageType.RESOLVED:
                            inner_emitted[0] = True
                            actions.down([(MessageType.RESOLVED,)])
                        elif t is MessageType.DIRTY:
                            actions.down([(MessageType.DIRTY,)])
                        elif t is MessageType.ERROR:
                            inner_unsub[0] = None
                            actions.down([m])
                        elif t is MessageType.COMPLETE:
                            inner_ended[0] = True
                            inner_unsub[0] = None
                            process_next()
                        else:
                            actions.down([m])

                inner_unsub[0] = inner.subscribe(inner_sink)
                if not inner_emitted[0] and not inner_ended[0]:
                    actions.emit(inner.get())
                if inner_ended[0]:
                    inner_unsub[0] = None

            def outer_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.DATA:
                        v = _msg_val(m)
                        if not inner_active[0]:
                            subscribe_inner(fn(v))
                        else:
                            if max_buffer > 0 and len(queue) >= max_buffer:
                                queue.popleft()
                            queue.append(v)
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        outer_done[0] = True
                        if not inner_active[0]:
                            teardown_inner()
                            actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        teardown_inner()
                        queue.clear()
                        actions.down([m])
                    else:
                        actions.down([m])

            outer_unsub = outer.subscribe(outer_sink)

            def cleanup() -> None:
                teardown_inner()
                queue.clear()
                outer_unsub()

            return cleanup

        opts: dict[str, Any] = {
            "describe_kind": "concat_map",
            "complete_when_deps_complete": False,
        }
        if has_initial:
            opts["initial"] = initial
        return node(start, **opts)

    return _op


def flat_map(fn: Callable[[Any], Node[Any]], *, initial: Any = _UNSET) -> PipeOperator:
    """Map each outer value to an inner node; subscribe to every inner concurrently (merge).

    Completes when the outer has completed and every inner subscription has ended.

    Args:
        fn: ``outer_value -> Node``.
        initial: Optional initial ``get()`` value.

    Returns:
        A unary pipe operator.
    """

    has_initial = initial is not _UNSET

    def _op(outer: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            inner_unsubs: list[Callable[[], None]] = []
            outer_done = [False]

            def subscribe_inner(inner: Node[Any]) -> None:
                inner_emitted = [False]
                inner_ended = [False]
                slot: list[Callable[[], None] | None] = [None]

                def inner_sink(msgs: Messages) -> None:
                    for m in msgs:
                        t = m[0]
                        if t is MessageType.DATA:
                            inner_emitted[0] = True
                            actions.emit(_msg_val(m))
                        elif t is MessageType.RESOLVED:
                            inner_emitted[0] = True
                            actions.down([(MessageType.RESOLVED,)])
                        elif t is MessageType.DIRTY:
                            actions.down([(MessageType.DIRTY,)])
                        elif t is MessageType.ERROR:
                            if slot[0] is not None:
                                with suppress(ValueError):
                                    inner_unsubs.remove(slot[0])
                                slot[0] = None
                            actions.down([m])
                        elif t is MessageType.COMPLETE:
                            inner_ended[0] = True
                            if slot[0] is not None:
                                with suppress(ValueError):
                                    inner_unsubs.remove(slot[0])
                                slot[0] = None
                            if outer_done[0] and len(inner_unsubs) == 0:
                                actions.down([(MessageType.COMPLETE,)])
                        else:
                            actions.down([m])

                u = inner.subscribe(inner_sink)
                slot[0] = u
                if not inner_ended[0]:
                    inner_unsubs.append(u)
                else:
                    if outer_done[0] and len(inner_unsubs) == 0:
                        actions.down([(MessageType.COMPLETE,)])
                if not inner_emitted[0] and not inner_ended[0]:
                    actions.emit(inner.get())

            def outer_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.DATA:
                        subscribe_inner(fn(_msg_val(m)))
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        outer_done[0] = True
                        if len(inner_unsubs) == 0:
                            actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        for u in list(inner_unsubs):
                            u()
                        inner_unsubs.clear()
                        actions.down([m])
                    else:
                        actions.down([m])

            outer_unsub = outer.subscribe(outer_sink)

            def cleanup() -> None:
                for u in list(inner_unsubs):
                    u()
                inner_unsubs.clear()
                outer_unsub()

            return cleanup

        opts: dict[str, Any] = {
            "describe_kind": "flat_map",
            "complete_when_deps_complete": False,
        }
        if has_initial:
            opts["initial"] = initial
        return node(start, **opts)

    return _op


def exhaust_map(fn: Callable[[Any], Node[Any]], *, initial: Any = _UNSET) -> PipeOperator:
    """Like :func:`switch_map`, but ignores new outer ``DATA`` while the current inner is active.

    Args:
        fn: ``outer_value -> Node``.
        initial: Optional initial ``get()`` value.

    Returns:
        A unary pipe operator.
    """

    has_initial = initial is not _UNSET

    def _op(outer: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            inner_unsub: list[Callable[[], None] | None] = [None]
            outer_done = [False]
            busy = [False]

            def teardown_inner() -> None:
                if inner_unsub[0] is not None:
                    inner_unsub[0]()
                    inner_unsub[0] = None

            def subscribe_inner(inner: Node[Any]) -> None:
                teardown_inner()
                busy[0] = True
                inner_emitted = [False]
                inner_ended = [False]

                def inner_sink(msgs: Messages) -> None:
                    for m in msgs:
                        t = m[0]
                        if t is MessageType.DATA:
                            inner_emitted[0] = True
                            actions.emit(_msg_val(m))
                        elif t is MessageType.RESOLVED:
                            inner_emitted[0] = True
                            actions.down([(MessageType.RESOLVED,)])
                        elif t is MessageType.DIRTY:
                            actions.down([(MessageType.DIRTY,)])
                        elif t is MessageType.ERROR:
                            busy[0] = False
                            inner_unsub[0] = None
                            actions.down([m])
                        elif t is MessageType.COMPLETE:
                            inner_ended[0] = True
                            busy[0] = False
                            inner_unsub[0] = None
                            if outer_done[0]:
                                actions.down([(MessageType.COMPLETE,)])
                        else:
                            actions.down([m])

                inner_unsub[0] = inner.subscribe(inner_sink)
                if not inner_emitted[0] and not inner_ended[0]:
                    actions.emit(inner.get())
                if inner_ended[0]:
                    busy[0] = False
                    inner_unsub[0] = None

            def outer_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.DATA:
                        if not busy[0]:
                            subscribe_inner(fn(_msg_val(m)))
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        outer_done[0] = True
                        if not busy[0]:
                            teardown_inner()
                            actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        teardown_inner()
                        actions.down([m])
                    else:
                        actions.down([m])

            outer_unsub = outer.subscribe(outer_sink)

            def cleanup() -> None:
                teardown_inner()
                outer_unsub()

            return cleanup

        opts: dict[str, Any] = {
            "describe_kind": "exhaust_map",
            "complete_when_deps_complete": False,
        }
        if has_initial:
            opts["initial"] = initial
        return node(start, **opts)

    return _op


# --- time / scheduling (threading.Timer) --------------------------------------


def debounce(seconds: float) -> PipeOperator:
    """Emit the latest upstream ``DATA`` only after ``seconds`` of silence; flush on ``COMPLETE``.

    Timer is cancelled on upstream ``ERROR`` or unsubscribe.
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            timer: list[threading.Timer | None] = [None]
            pending: list[Any] = [None]
            has_pending = [False]

            def cancel_timer() -> None:
                if timer[0] is not None:
                    timer[0].cancel()
                    timer[0] = None

            def outer_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        cancel_timer()
                        pending[0] = _msg_val(m)
                        has_pending[0] = True

                        def flush() -> None:
                            timer[0] = None
                            if not has_pending[0]:
                                return
                            v = pending[0]
                            has_pending[0] = False
                            actions.emit(v)

                        tt = threading.Timer(seconds, flush)
                        tt.daemon = True
                        tt.start()
                        timer[0] = tt
                    elif t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        cancel_timer()
                        if has_pending[0]:
                            has_pending[0] = False
                            actions.emit(pending[0])
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        cancel_timer()
                        has_pending[0] = False
                        actions.down([m])
                    else:
                        actions.down([m])

            outer_unsub = src.subscribe(outer_sink)

            def cleanup() -> None:
                cancel_timer()
                outer_unsub()

            return cleanup

        return node(start, describe_kind="debounce", complete_when_deps_complete=False)

    return _op


def throttle(
    seconds: float, *, leading: bool = True, trailing: bool = False
) -> PipeOperator:
    """Rate-limit: at most one emit per ``seconds`` window.

    Args:
        seconds: Window length in seconds.
        leading: Whether to emit the first value at the start of each window (default ``True``).
        trailing: Whether to emit the latest suppressed value when the window closes.
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            window: list[threading.Timer | None] = [None]
            latest: list[Any] = [None]
            had_trailing_candidate = [False]

            def cancel_window() -> None:
                if window[0] is not None:
                    window[0].cancel()
                    window[0] = None

            def outer_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        v = _msg_val(m)
                        latest[0] = v
                        if window[0] is not None:
                            had_trailing_candidate[0] = True
                            continue
                        if leading:
                            actions.emit(v)
                        else:
                            had_trailing_candidate[0] = True
                        def close_window() -> None:
                            window[0] = None
                            if trailing and had_trailing_candidate[0]:
                                actions.emit(latest[0])
                                had_trailing_candidate[0] = False

                        tt = threading.Timer(seconds, close_window)
                        tt.daemon = True
                        tt.start()
                        window[0] = tt
                    elif t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        cancel_window()
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        cancel_window()
                        actions.down([m])
                    else:
                        actions.down([m])

            outer_unsub = src.subscribe(outer_sink)

            def cleanup() -> None:
                cancel_window()
                outer_unsub()

            return cleanup

        return node(
            start,
            describe_kind="throttle",
            complete_when_deps_complete=False,
        )

    return _op


def sample(notifier: Node[Any]) -> PipeOperator:
    """Emit the primary's latest ``get()`` whenever ``notifier`` settles with ``DATA``.

    A mirror node follows the primary so ``get()`` on the output reflects the last sampled
    value; the latest primary value before a sample is read via an internal pass-through node.
    """

    def _op(src: Node[Any]) -> Node[Any]:
        mirror = node([src], lambda d, _: d[0], describe_kind="sample_mirror")

        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            def in_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.COMPLETE:
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        actions.down([m])

            def n_sink(msgs: Messages) -> None:
                for m in msgs:
                    if m[0] is MessageType.DATA:
                        actions.emit(mirror.get())
                    elif m[0] is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif m[0] is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif m[0] is MessageType.COMPLETE:
                        actions.down([(MessageType.COMPLETE,)])
                    elif m[0] is MessageType.ERROR:
                        actions.down([m])
                    else:
                        actions.down([m])

            u0 = mirror.subscribe(in_sink)
            u1 = notifier.subscribe(n_sink)

            def cleanup() -> None:
                u0()
                u1()

            return cleanup

        return node(
            start,
            initial=src.get(),
            describe_kind="sample",
            complete_when_deps_complete=False,
        )

    return _op


def audit(seconds: float) -> PipeOperator:
    """Trailing-only window: after each ``DATA``, wait ``seconds``, then emit the latest value.

    Each ``DATA`` stores the latest value and restarts the timer. When the timer fires,
    the stored value is emitted. No leading-edge emission (Rx ``auditTime`` semantics).
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            timer: list[threading.Timer | None] = [None]
            latest: list[Any] = [None]
            has = [False]

            def cancel_timer() -> None:
                if timer[0] is not None:
                    timer[0].cancel()
                    timer[0] = None

            def outer_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        latest[0] = _msg_val(m)
                        has[0] = True
                        cancel_timer()

                        def fire() -> None:
                            timer[0] = None
                            if has[0]:
                                has[0] = False
                                actions.emit(latest[0])

                        tt = threading.Timer(seconds, fire)
                        tt.daemon = True
                        tt.start()
                        timer[0] = tt
                    elif t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        cancel_timer()
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        cancel_timer()
                        actions.down([m])
                    else:
                        actions.down([m])

            outer_unsub = src.subscribe(outer_sink)

            def cleanup() -> None:
                cancel_timer()
                outer_unsub()

            return cleanup

        return node(start, describe_kind="audit", complete_when_deps_complete=False)

    return _op


def delay(seconds: float) -> PipeOperator:
    """Delay each ``DATA`` by ``seconds`` (one timer per pending value, FIFO)."""

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            timers: list[threading.Timer] = []

            def outer_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        v = _msg_val(m)

                        def fire(val: Any = v) -> None:
                            with suppress(IndexError):
                                timers.pop(0)
                            actions.emit(val)

                        tt = threading.Timer(seconds, fire)
                        tt.daemon = True
                        timers.append(tt)
                        tt.start()
                    elif t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        for tt in timers:
                            tt.cancel()
                        timers.clear()
                        actions.down([m])
                    else:
                        actions.down([m])

            outer_unsub = src.subscribe(outer_sink)

            def cleanup() -> None:
                for tt in timers:
                    tt.cancel()
                timers.clear()
                outer_unsub()

            return cleanup

        return node(start, describe_kind="delay", complete_when_deps_complete=False)

    return _op


def timeout(seconds: float, *, error: BaseException | None = None) -> PipeOperator:
    """Emit ``ERROR`` if no ``DATA`` within ``seconds`` after subscribe or last ``DATA``.

    Timer resets on each ``DATA``; unsubscribe cancels the watchdog.
    """

    err = error if error is not None else TimeoutError("timeout")

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            timer: list[threading.Timer | None] = [None]

            def cancel_timer() -> None:
                if timer[0] is not None:
                    timer[0].cancel()
                    timer[0] = None

            def schedule() -> None:
                cancel_timer()

                def fire() -> None:
                    timer[0] = None
                    actions.down([(MessageType.ERROR, err)])

                tt = threading.Timer(seconds, fire)
                tt.daemon = True
                tt.start()
                timer[0] = tt

            schedule()

            def outer_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        cancel_timer()
                        actions.emit(_msg_val(m))
                        schedule()
                    elif t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        cancel_timer()
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        cancel_timer()
                        actions.down([m])
                    else:
                        actions.down([m])

            outer_unsub = src.subscribe(outer_sink)

            def cleanup() -> None:
                cancel_timer()
                outer_unsub()

            return cleanup

        return node(start, describe_kind="timeout", complete_when_deps_complete=False)

    return _op


# --- buffers ------------------------------------------------------------------


def buffer(notifier: Node[Any]) -> PipeOperator:
    """Collect ``DATA`` values in a list; emit that list when ``notifier`` emits ``DATA``."""

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            buf: list[Any] = []

            def src_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        buf.append(_msg_val(m))
                    elif t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        buf.clear()
                        actions.down([m])
                    else:
                        actions.down([m])

            def n_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        if buf:
                            actions.emit(list(buf))
                            buf.clear()
                    elif t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        buf.clear()
                        actions.down([m])
                    else:
                        actions.down([m])

            u0 = src.subscribe(src_sink)
            u1 = notifier.subscribe(n_sink)

            def cleanup() -> None:
                buf.clear()
                u0()
                u1()

            return cleanup

        return node(start, describe_kind="buffer", complete_when_deps_complete=False)

    return _op


def buffer_count(n: int) -> PipeOperator:
    """Emit a list of every ``n`` consecutive ``DATA`` values."""

    if n <= 0:
        msg = "buffer_count expects n > 0"
        raise ValueError(msg)

    def _op(src: Node[Any]) -> Node[Any]:
        acc: list[Any] = []

        def compute(_deps: list[Any], actions: NodeActions) -> Any:
            return None

        def on_message(msg: Any, _index: int, actions: NodeActions) -> bool:
            t = msg[0]
            if t is MessageType.DATA:
                acc.append(_msg_val(msg))
                if len(acc) >= n:
                    actions.emit(list(acc))
                    acc.clear()
                return True
            if t is MessageType.DIRTY:
                actions.down([(MessageType.DIRTY,)])
                return True
            if t is MessageType.RESOLVED:
                actions.down([(MessageType.RESOLVED,)])
                return True
            if t is MessageType.COMPLETE:
                if acc:
                    actions.emit(list(acc))
                    acc.clear()
                actions.down([(MessageType.COMPLETE,)])
                return True
            if t is MessageType.ERROR:
                acc.clear()
                actions.down([msg])
                return True
            actions.down([msg])
            return True

        return node(
            [src],
            compute,
            on_message=on_message,
            describe_kind="buffer_count",
            complete_when_deps_complete=False,
        )

    return _op


def buffer_time(seconds: float) -> PipeOperator:
    """Emit a list of ``DATA`` values collected over each wall-clock window of ``seconds``."""

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            buf: list[Any] = []
            timer: list[threading.Timer | None] = [None]

            def cancel() -> None:
                if timer[0] is not None:
                    timer[0].cancel()
                    timer[0] = None

            def flush() -> None:
                timer[0] = None
                if buf:
                    actions.emit(list(buf))
                    buf.clear()

            def arm() -> None:
                cancel()
                tt = threading.Timer(seconds, flush)
                tt.daemon = True
                tt.start()
                timer[0] = tt

            arm()

            def outer_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        buf.append(_msg_val(m))
                    elif t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        cancel()
                        flush()
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        cancel()
                        buf.clear()
                        actions.down([m])
                    else:
                        actions.down([m])

            outer_unsub = src.subscribe(outer_sink)

            def cleanup() -> None:
                cancel()
                buf.clear()
                outer_unsub()

            return cleanup

        return node(start, describe_kind="buffer_time", complete_when_deps_complete=False)

    return _op


# --- sources / misc -----------------------------------------------------------


def interval(seconds: float) -> Node[Any]:
    """Producer that emits ``0, 1, 2, …`` on a fixed timer interval."""

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        n = [0]
        timer: list[threading.Timer | None] = [None]
        stopped = [False]

        def cancel() -> None:
            if timer[0] is not None:
                timer[0].cancel()
                timer[0] = None

        def tick() -> None:
            if stopped[0]:
                return
            actions.emit(n[0])
            n[0] += 1
            tt = threading.Timer(seconds, tick)
            tt.daemon = True
            tt.start()
            timer[0] = tt

        tt0 = threading.Timer(seconds, tick)
        tt0.daemon = True
        tt0.start()
        timer[0] = tt0

        def cleanup() -> None:
            stopped[0] = True
            cancel()

        return cleanup

    # No ``initial``: first tick emits ``0``; matching ``initial=0`` would coalesce to RESOLVED.
    return node(start, describe_kind="interval", complete_when_deps_complete=False)


def repeat(times: int) -> PipeOperator:
    """Play the source to ``COMPLETE``, then re-subscribe, ``times`` times total.

    Each pass ends when the source emits ``COMPLETE``; the operator then subscribes again
    until ``times`` passes have finished.
    """

    if times <= 0:
        msg = "repeat expects times > 0"
        raise ValueError(msg)

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            remaining = [times]
            outer_unsub: list[Callable[[], None] | None] = [None]

            def detach() -> None:
                if outer_unsub[0] is not None:
                    outer_unsub[0]()
                    outer_unsub[0] = None

            def attach() -> None:
                detach()

                def outer_sink(msgs: Messages) -> None:
                    for m in msgs:
                        t = m[0]
                        if t is MessageType.DATA:
                            actions.emit(_msg_val(m))
                        elif t is MessageType.DIRTY:
                            actions.down([(MessageType.DIRTY,)])
                        elif t is MessageType.RESOLVED:
                            actions.down([(MessageType.RESOLVED,)])
                        elif t is MessageType.COMPLETE:
                            remaining[0] -= 1
                            detach()
                            if remaining[0] <= 0:
                                actions.down([(MessageType.COMPLETE,)])
                            else:
                                attach()
                        elif t is MessageType.ERROR:
                            detach()
                            actions.down([m])
                        else:
                            actions.down([m])

                outer_unsub[0] = src.subscribe(outer_sink)

            attach()

            def cleanup() -> None:
                detach()

            return cleanup

        return node(start, describe_kind="repeat", complete_when_deps_complete=False)

    return _op


def gate(control: Node[Any]) -> PipeOperator:
    """Forward ``DATA`` only when ``control`` is truthy; otherwise emit ``RESOLVED``.

    This is a value-level gate (boolean control signal).  See :func:`pausable` for
    a protocol-level ``PAUSE``/``RESUME`` buffer.
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def compute(deps: list[Any], actions: NodeActions) -> Any:
            if not deps[1]:
                actions.down([(MessageType.RESOLVED,)])
                return None
            return deps[0]

        return node([src, control], compute, describe_kind="gate")

    return _op


def pausable() -> PipeOperator:
    """Buffer ``DIRTY``/``DATA``/``RESOLVED`` while ``PAUSE`` is in effect; flush on ``RESUME``.

    Protocol-level pause/resume using ``PAUSE``/``RESUME`` message types. Matches
    TypeScript ``pausable`` semantics.
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            paused = [False]
            backlog: list[Any] = []

            def outer_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.PAUSE:
                        paused[0] = True
                        actions.down([m])
                    elif t is MessageType.RESUME:
                        paused[0] = False
                        actions.down([m])
                        for bm in backlog:
                            actions.down([bm])
                        backlog.clear()
                    elif paused[0] and t in (
                        MessageType.DIRTY,
                        MessageType.DATA,
                        MessageType.RESOLVED,
                    ):
                        backlog.append(m)
                    else:
                        actions.down([m])

            outer_unsub = src.subscribe(outer_sink)

            def cleanup() -> None:
                backlog.clear()
                outer_unsub()

            return cleanup

        return node(start, describe_kind="pausable", complete_when_deps_complete=False)

    return _op


def rescue(recover: Callable[[BaseException], Any]) -> PipeOperator:
    """Turn upstream ``ERROR`` into a normal ``DATA`` from ``recover(exc)``."""

    def _op(src: Node[Any]) -> Node[Any]:
        def compute(deps: list[Any], _actions: NodeActions) -> Any:
            return deps[0]

        def on_message(msg: Any, _index: int, actions: NodeActions) -> bool:
            if msg[0] is MessageType.ERROR:
                try:
                    actions.emit(recover(_msg_val(msg)))
                except BaseException as err:  # noqa: BLE001 — re-raise as ERROR
                    actions.down([(MessageType.ERROR, err)])
                return True
            return False

        return node(
            [src],
            compute,
            on_message=on_message,
            describe_kind="rescue",
            complete_when_deps_complete=False,
        )

    return _op


# --- window operators (true sub-node windows) --------------------------------


def window(notifier: Node[Any]) -> PipeOperator:
    """Split source ``DATA`` into sub-node windows; new window on each notifier ``DATA``.

    Each emitted value is a :class:`~graphrefly.core.node.Node` that receives
    the values belonging to that window.
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            win_actions: list[NodeActions | None] = [None]
            win_unsub: list[Callable[[], None] | None] = [None]

            def close_win() -> None:
                if win_actions[0] is not None:
                    win_actions[0].down([(MessageType.COMPLETE,)])
                win_actions[0] = None
                if win_unsub[0] is not None:
                    win_unsub[0]()
                    win_unsub[0] = None

            def open_win() -> None:
                holder: list[NodeActions | None] = [None]

                def win_start(_d: list[Any], wa: NodeActions) -> Callable[[], None]:
                    holder[0] = wa
                    return lambda: None

                w = node(win_start, describe_kind="window_inner", complete_when_deps_complete=False)
                win_unsub[0] = w.subscribe(lambda _msgs: None)
                win_actions[0] = holder[0]
                actions.emit(w)

            def src_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        if win_actions[0] is None:
                            open_win()
                        if win_actions[0] is not None:
                            win_actions[0].down([(MessageType.DATA, _msg_val(m))])
                    elif t is MessageType.COMPLETE:
                        close_win()
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        if win_actions[0] is not None:
                            win_actions[0].down([m])
                        win_actions[0] = None
                        actions.down([m])
                    elif t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    else:
                        actions.down([m])

            def n_sink(msgs: Messages) -> None:
                for m in msgs:
                    if m[0] is MessageType.DATA:
                        close_win()
                        open_win()

            u0 = src.subscribe(src_sink)
            u1 = notifier.subscribe(n_sink)

            def cleanup() -> None:
                close_win()
                u0()
                u1()

            return cleanup

        return node(start, describe_kind="window", complete_when_deps_complete=False)

    return _op


def window_count(n: int) -> PipeOperator:
    """Split source ``DATA`` into sub-node windows of ``n`` items each."""

    if n <= 0:
        msg = "window_count expects n > 0"
        raise ValueError(msg)

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            win_actions: list[NodeActions | None] = [None]
            win_unsub: list[Callable[[], None] | None] = [None]
            count = [0]

            def close_win() -> None:
                if win_actions[0] is not None:
                    win_actions[0].down([(MessageType.COMPLETE,)])
                win_actions[0] = None
                if win_unsub[0] is not None:
                    win_unsub[0]()
                    win_unsub[0] = None

            def open_win() -> None:
                holder: list[NodeActions | None] = [None]

                def win_start(_d: list[Any], wa: NodeActions) -> Callable[[], None]:
                    holder[0] = wa
                    return lambda: None

                w = node(win_start, describe_kind="window_inner", complete_when_deps_complete=False)
                win_unsub[0] = w.subscribe(lambda _msgs: None)
                win_actions[0] = holder[0]
                count[0] = 0
                actions.emit(w)

            def outer_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        if win_actions[0] is None:
                            open_win()
                        if win_actions[0] is not None:
                            win_actions[0].down([(MessageType.DATA, _msg_val(m))])
                        count[0] += 1
                        if count[0] >= n:
                            close_win()
                    elif t is MessageType.COMPLETE:
                        close_win()
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        if win_actions[0] is not None:
                            win_actions[0].down([m])
                        win_actions[0] = None
                        actions.down([m])
                    elif t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    else:
                        actions.down([m])

            outer_unsub = src.subscribe(outer_sink)

            def cleanup() -> None:
                close_win()
                outer_unsub()

            return cleanup

        return node(start, describe_kind="window_count", complete_when_deps_complete=False)

    return _op


def window_time(seconds: float) -> PipeOperator:
    """Split source ``DATA`` into sub-node windows, each lasting ``seconds``."""

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            win_actions: list[NodeActions | None] = [None]
            win_unsub: list[Callable[[], None] | None] = [None]
            timer: list[threading.Timer | None] = [None]

            def close_win() -> None:
                if win_actions[0] is not None:
                    win_actions[0].down([(MessageType.COMPLETE,)])
                win_actions[0] = None
                if win_unsub[0] is not None:
                    win_unsub[0]()
                    win_unsub[0] = None

            def open_win() -> None:
                holder: list[NodeActions | None] = [None]

                def win_start(_d: list[Any], wa: NodeActions) -> Callable[[], None]:
                    holder[0] = wa
                    return lambda: None

                w = node(win_start, describe_kind="window_inner", complete_when_deps_complete=False)
                win_unsub[0] = w.subscribe(lambda _msgs: None)
                win_actions[0] = holder[0]
                actions.emit(w)

            def rotate() -> None:
                close_win()
                open_win()
                arm()

            def arm() -> None:
                if timer[0] is not None:
                    timer[0].cancel()
                tt = threading.Timer(seconds, rotate)
                tt.daemon = True
                tt.start()
                timer[0] = tt

            open_win()
            arm()

            def outer_sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        if win_actions[0] is not None:
                            win_actions[0].down([(MessageType.DATA, _msg_val(m))])
                    elif t is MessageType.COMPLETE:
                        if timer[0] is not None:
                            timer[0].cancel()
                            timer[0] = None
                        close_win()
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        if timer[0] is not None:
                            timer[0].cancel()
                            timer[0] = None
                        if win_actions[0] is not None:
                            win_actions[0].down([m])
                        win_actions[0] = None
                        actions.down([m])
                    elif t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    else:
                        actions.down([m])

            outer_unsub = src.subscribe(outer_sink)

            def cleanup() -> None:
                if timer[0] is not None:
                    timer[0].cancel()
                    timer[0] = None
                close_win()
                outer_unsub()

            return cleanup

        return node(start, describe_kind="window_time", complete_when_deps_complete=False)

    return _op


__all__ = [
    "audit",
    "buffer",
    "buffer_count",
    "buffer_time",
    "concat_map",
    "debounce",
    "delay",
    "exhaust_map",
    "flat_map",
    "gate",
    "interval",
    "pausable",
    "repeat",
    "rescue",
    "sample",
    "switch_map",
    "throttle",
    "timeout",
    "window",
    "window_count",
    "window_time",
]
