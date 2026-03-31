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
from graphrefly.extra.sources import from_any

if TYPE_CHECKING:
    from collections.abc import Callable

    from graphrefly.core.sugar import PipeOperator

_UNSET: Any = object()


def _msg_val(m: tuple[Any, ...]) -> Any:
    """Payload for a ``DATA`` / ``ERROR`` tuple (GraphReFly messages are at least two elements)."""
    assert len(m) >= 2
    return m[1]


def _as_node(value: Any) -> Node[Any]:
    """Coerce mapper outputs to Node via ``from_any`` (roadmap §3.1b)."""
    return from_any(value)


# --- dynamic inner subscription (switch / concat / flat / exhaust) ------------


def _forward_inner(
    inner: Node[Any],
    actions: NodeActions,
    on_inner_complete: Callable[[], None],
) -> Callable[[], None]:
    """Subscribe to *inner*, forwarding all messages except COMPLETE to *actions*.

    On inner COMPLETE, calls *on_inner_complete* (but does NOT forward the COMPLETE
    message itself — the caller decides when the output completes).
    On inner ERROR, forwards the error then calls *on_inner_complete*.

    Returns an unsubscribe callable.

    Matches TS ``forwardInner``.
    """
    unsub: Callable[[], None] | None = None
    finished = False
    emitted = False

    def finish() -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        on_inner_complete()

    def inner_sink(msgs: Messages) -> None:
        nonlocal emitted
        saw_complete = False
        saw_error = False
        out: Messages = []
        for m in msgs:
            if m[0] is MessageType.COMPLETE:
                saw_complete = True
            else:
                if m[0] is MessageType.DATA:
                    emitted = True
                if m[0] is MessageType.ERROR:
                    saw_error = True
                out.append(m)
        if out:
            actions.down(out)
        if saw_error or saw_complete:
            finish()

    unsub = inner.subscribe(inner_sink)

    # Emit inner's current value only if subscribe didn't already emit DATA.
    # Source nodes (state) don't emit DATA on subscribe, but their value
    # is already settled. Derived nodes that compute during subscribe will
    # have set emitted=True via inner_sink, so we skip the manual emit.
    # None is a valid DATA payload (Node[None] / void sources).
    if unsub is not None and not emitted and inner.status in ("settled", "resolved"):
        actions.down([(MessageType.DATA, inner.get())])

    if inner.status in ("completed", "errored"):
        finish()

    def stop() -> None:
        nonlocal unsub
        if unsub is not None:
            unsub()
            unsub = None

    return stop


def switch_map(
    fn: Callable[[Any], Any],
    *,
    initial: Any = _UNSET,
) -> PipeOperator:
    """Map each outer settled value to an inner node; keep only the latest inner subscription.

    On each outer ``DATA``, the previous inner is unsubscribed. Inner ``DATA`` / ``RESOLVED`` /
    ``DIRTY`` are forwarded; inner ``ERROR`` always terminates; inner ``COMPLETE`` completes
    the output only if the outer has already completed.

    Args:
        fn: ``outer_value -> source`` for the active inner. Return ``Node``, scalar,
            awaitable, iterable, or async iterable (coerced via
            :func:`graphrefly.extra.sources.from_any`).
        initial: Optional seed for :meth:`~graphrefly.core.node.Node.get` before the first inner
            emission.

    Returns:
        A unary pipe operator ``(Node) -> Node``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import switch_map
        from graphrefly.extra import of
        src = state(1)
        out = pipe(src, switch_map(lambda v: of(v * 10)))
        ```
    """

    has_initial = initial is not _UNSET

    def _op(outer: Node[Any]) -> Node[Any]:
        inner_unsub: Callable[[], None] | None = None
        source_done = False
        attached = False

        def clear_inner() -> None:
            nonlocal inner_unsub
            if inner_unsub is not None:
                inner_unsub()
                inner_unsub = None

        def attach(v: Any, a: NodeActions) -> None:
            nonlocal attached, inner_unsub
            attached = True
            clear_inner()

            def _on_inner_complete() -> None:
                clear_inner()
                if source_done:
                    a.down([(MessageType.COMPLETE,)])

            inner_unsub = _forward_inner(_as_node(fn(v)), a, _on_inner_complete)

        def compute(deps: list[Any], a: NodeActions) -> Any:
            if not attached:
                attach(deps[0], a)
            return clear_inner

        def on_message(msg: Any, _index: int, a: NodeActions) -> bool:
            nonlocal source_done
            t = msg[0]
            if t is MessageType.ERROR:
                clear_inner()
                a.down([msg])
                return True
            if t is MessageType.COMPLETE:
                source_done = True
                if inner_unsub is None:
                    a.down([(MessageType.COMPLETE,)])
                return True
            if t is MessageType.DIRTY:
                a.down([(MessageType.DIRTY,)])
                return True
            if t is MessageType.RESOLVED:
                a.down([(MessageType.RESOLVED,)])
                return True
            if t is MessageType.DATA:
                attach(_msg_val(msg), a)
                return True
            return False

        opts: dict[str, Any] = {
            "describe_kind": "switch_map",
            "complete_when_deps_complete": False,
            "on_message": on_message,
        }
        if has_initial:
            opts["initial"] = initial
        return node([outer], compute, **opts)

    return _op


def concat_map(
    fn: Callable[[Any], Any],
    *,
    initial: Any = _UNSET,
    max_buffer: int = 0,
) -> PipeOperator:
    """Map outer values to inner nodes; run inners strictly one after another.

    While an inner is active, outer ``DATA`` values are queued. ``max_buffer > 0`` drops the
    oldest queued value when the queue would exceed that length.

    Args:
        fn: ``outer_value -> source`` (coerced via :func:`graphrefly.extra.sources.from_any`).
        initial: Optional initial ``get()`` value.
        max_buffer: Maximum queued outer keys (``0`` = unlimited).

    Returns:
        A unary pipe operator ``(Node) -> Node``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import concat_map
        from graphrefly.extra import of
        src = state(1)
        out = pipe(src, concat_map(lambda v: of(v, v + 1)))
        ```
    """

    has_initial = initial is not _UNSET

    def _op(outer: Node[Any]) -> Node[Any]:
        queue: deque[Any] = deque()
        inner_unsub: Callable[[], None] | None = None
        source_done = False
        attached = False

        def clear_inner() -> None:
            nonlocal inner_unsub
            if inner_unsub is not None:
                inner_unsub()
                inner_unsub = None

        def try_pump(a: NodeActions) -> None:
            nonlocal inner_unsub
            if inner_unsub is not None:
                return
            if len(queue) == 0:
                if source_done:
                    a.down([(MessageType.COMPLETE,)])
                return
            v = queue.popleft()

            def _on_inner_complete() -> None:
                clear_inner()
                try_pump(a)

            inner_unsub = _forward_inner(_as_node(fn(v)), a, _on_inner_complete)

        def enqueue(v: Any, a: NodeActions) -> None:
            nonlocal attached
            attached = True
            if max_buffer > 0 and len(queue) >= max_buffer:
                queue.popleft()
            queue.append(v)
            try_pump(a)

        def compute(deps: list[Any], a: NodeActions) -> Any:
            if not attached:
                enqueue(deps[0], a)
            return clear_inner

        def on_message(msg: Any, _index: int, a: NodeActions) -> bool:
            nonlocal source_done
            t = msg[0]
            if t is MessageType.ERROR:
                clear_inner()
                queue.clear()
                a.down([msg])
                return True
            if t is MessageType.COMPLETE:
                source_done = True
                try_pump(a)
                return True
            if t is MessageType.DIRTY:
                a.down([(MessageType.DIRTY,)])
                return True
            if t is MessageType.RESOLVED:
                a.down([(MessageType.RESOLVED,)])
                return True
            if t is MessageType.DATA:
                enqueue(_msg_val(msg), a)
                return True
            return False

        opts: dict[str, Any] = {
            "describe_kind": "concat_map",
            "complete_when_deps_complete": False,
            "on_message": on_message,
        }
        if has_initial:
            opts["initial"] = initial
        return node([outer], compute, **opts)

    return _op


def flat_map(
    fn: Callable[[Any], Any],
    *,
    initial: Any = _UNSET,
    concurrent: int | None = None,
) -> PipeOperator:
    """Map each outer value to an inner node; subscribe to every inner concurrently (merge).

    Completes when the outer has completed and every inner subscription has ended.

    Args:
        fn: ``outer_value -> source`` (coerced via :func:`graphrefly.extra.sources.from_any`).
        initial: Optional initial ``get()`` value.
        concurrent: When set, limit the number of concurrently active inner subscriptions.
            Outer values beyond this limit are buffered and drained as inner subscriptions
            complete.

    Returns:
        A unary pipe operator ``(Node) -> Node``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import flat_map
        from graphrefly.extra import of
        src = state(1)
        out = pipe(src, flat_map(lambda v: of(v * 2)))
        ```
    """

    has_initial = initial is not _UNSET
    max_concurrent = float("inf") if concurrent is None else max(concurrent, 1)

    def _op(outer: Node[Any]) -> Node[Any]:
        active = 0
        source_done = False
        inner_stops: list[Callable[[], None]] = []
        buffer: deque[Any] = deque()
        attached = False

        def try_complete(a: NodeActions) -> None:
            if source_done and active == 0 and len(buffer) == 0:
                a.down([(MessageType.COMPLETE,)])

        def spawn(v: Any, a: NodeActions) -> None:
            nonlocal active
            active += 1
            stop: Callable[[], None] | None = None

            def on_done() -> None:
                nonlocal stop, active
                if stop is not None:
                    with suppress(ValueError):
                        inner_stops.remove(stop)
                    stop = None
                active -= 1
                drain_buffer(a)
                try_complete(a)

            stop = _forward_inner(_as_node(fn(v)), a, on_done)
            inner_stops.append(stop)

        def drain_buffer(a: NodeActions) -> None:
            while buffer and active < max_concurrent:
                spawn(buffer.popleft(), a)

        def enqueue(v: Any, a: NodeActions) -> None:
            if active < max_concurrent:
                spawn(v, a)
            else:
                buffer.append(v)

        def clear_all() -> None:
            nonlocal active
            for u in list(inner_stops):
                u()
            inner_stops.clear()
            active = 0
            buffer.clear()

        def compute(deps: list[Any], a: NodeActions) -> Any:
            nonlocal attached
            if not attached:
                attached = True
                enqueue(deps[0], a)
            return clear_all

        def on_message(msg: Any, _index: int, a: NodeActions) -> bool:
            nonlocal source_done
            t = msg[0]
            if t is MessageType.ERROR:
                clear_all()
                a.down([msg])
                return True
            if t is MessageType.COMPLETE:
                source_done = True
                try_complete(a)
                return True
            if t is MessageType.DIRTY:
                a.down([(MessageType.DIRTY,)])
                return True
            if t is MessageType.RESOLVED:
                a.down([(MessageType.RESOLVED,)])
                return True
            if t is MessageType.DATA:
                enqueue(_msg_val(msg), a)
                return True
            return False

        opts: dict[str, Any] = {
            "describe_kind": "flat_map",
            "complete_when_deps_complete": False,
            "on_message": on_message,
        }
        if has_initial:
            opts["initial"] = initial
        return node([outer], compute, **opts)

    return _op


def exhaust_map(fn: Callable[[Any], Any], *, initial: Any = _UNSET) -> PipeOperator:
    """Like :func:`switch_map`, but ignores new outer ``DATA`` while the current inner is active.

    Args:
        fn: ``outer_value -> source`` (coerced via :func:`graphrefly.extra.sources.from_any`).
        initial: Optional initial ``get()`` value.

    Returns:
        A unary pipe operator ``(Node) -> Node``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import exhaust_map
        from graphrefly.extra import of
        src = state(1)
        out = pipe(src, exhaust_map(lambda v: of(v)))
        ```
    """

    has_initial = initial is not _UNSET

    def _op(outer: Node[Any]) -> Node[Any]:
        inner_unsub: Callable[[], None] | None = None
        source_done = False
        attached = False

        def clear_inner() -> None:
            nonlocal inner_unsub
            if inner_unsub is not None:
                inner_unsub()
                inner_unsub = None

        def attach(v: Any, a: NodeActions) -> None:
            nonlocal attached, inner_unsub
            attached = True

            def _on_inner_complete() -> None:
                clear_inner()
                if source_done:
                    a.down([(MessageType.COMPLETE,)])

            inner_unsub = _forward_inner(_as_node(fn(v)), a, _on_inner_complete)

        def compute(deps: list[Any], a: NodeActions) -> Any:
            if not attached and inner_unsub is None:
                attach(deps[0], a)
            return clear_inner

        def on_message(msg: Any, _index: int, a: NodeActions) -> bool:
            nonlocal source_done
            t = msg[0]
            if t is MessageType.ERROR:
                clear_inner()
                a.down([msg])
                return True
            if t is MessageType.COMPLETE:
                source_done = True
                if inner_unsub is None:
                    a.down([(MessageType.COMPLETE,)])
                return True
            if t is MessageType.DIRTY:
                a.down([(MessageType.DIRTY,)])
                return True
            if t is MessageType.RESOLVED:
                a.down([(MessageType.RESOLVED,)])
                return True
            if t is MessageType.DATA:
                if inner_unsub is not None:
                    a.down([(MessageType.RESOLVED,)])
                    return True
                attach(_msg_val(msg), a)
                return True
            return False

        opts: dict[str, Any] = {
            "describe_kind": "exhaust_map",
            "complete_when_deps_complete": False,
            "on_message": on_message,
        }
        if has_initial:
            opts["initial"] = initial
        return node([outer], compute, **opts)

    return _op


# --- time / scheduling (threading.Timer) --------------------------------------


def debounce(seconds: float) -> PipeOperator:
    """Emit the latest upstream ``DATA`` only after ``seconds`` of silence; flush on ``COMPLETE``.

    Timer is cancelled on upstream ``ERROR`` or unsubscribe.

    Args:
        seconds: Silence window in seconds; timer resets on each upstream ``DATA``.

    Returns:
        A unary pipe operator ``(Node) -> Node``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import debounce
        src = state(0)
        out = pipe(src, debounce(0.05))
        ```
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            timer: threading.Timer | None = None
            pending: Any = None
            has_pending = False

            def cancel_timer() -> None:
                nonlocal timer
                if timer is not None:
                    timer.cancel()
                    timer = None

            def flush() -> None:
                nonlocal timer, has_pending
                timer = None
                if not has_pending:
                    return
                v = pending
                has_pending = False
                actions.emit(v)

            def outer_sink(msgs: Messages) -> None:
                nonlocal timer, pending, has_pending
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        cancel_timer()
                        pending = _msg_val(m)
                        has_pending = True
                        tt = threading.Timer(seconds, flush)
                        tt.daemon = True
                        tt.start()
                        timer = tt
                    elif t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        cancel_timer()
                        if has_pending:
                            has_pending = False
                            actions.emit(pending)
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        cancel_timer()
                        has_pending = False
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


def throttle(seconds: float, *, leading: bool = True, trailing: bool = False) -> PipeOperator:
    """Rate-limit: emit at most one ``DATA`` per ``seconds`` window.

    Args:
        seconds: Window length in seconds.
        leading: Emit the first value at the window start (default ``True``).
        trailing: Emit the latest suppressed value when the window closes.

    Returns:
        A unary pipe operator ``(Node) -> Node``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import throttle
        src = state(0)
        out = pipe(src, throttle(0.1))
        ```
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            window: threading.Timer | None = None
            latest: Any = None
            had_trailing_candidate = False

            def cancel_window() -> None:
                nonlocal window
                if window is not None:
                    window.cancel()
                    window = None

            def close_window() -> None:
                nonlocal window, had_trailing_candidate
                window = None
                if trailing and had_trailing_candidate:
                    actions.emit(latest)
                    had_trailing_candidate = False

            def outer_sink(msgs: Messages) -> None:
                nonlocal window, latest, had_trailing_candidate
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        v = _msg_val(m)
                        latest = v
                        if window is not None:
                            had_trailing_candidate = True
                            continue
                        if leading:
                            actions.emit(v)
                        else:
                            had_trailing_candidate = True
                        tt = threading.Timer(seconds, close_window)
                        tt.daemon = True
                        tt.start()
                        window = tt
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
    """Emit the primary's latest value whenever ``notifier`` settles with ``DATA``.

    Source messages are intercepted via ``on_message``; only notifier ``DATA``
    (dep index 1) triggers ``src.get()`` emission. Matches TS ``sample`` architecture.

    Args:
        notifier: Node whose ``DATA`` triggers sampling of the primary's latest value.

    Returns:
        A unary pipe operator ``(Node) -> Node``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import sample
        src = state(0)
        tick = state(None)
        out = pipe(src, sample(tick))
        ```
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def compute(_deps: list[Any], _a: NodeActions) -> Any:
            return None

        def on_message(msg: Any, index: int, a: NodeActions) -> bool:
            t = msg[0]
            if t is MessageType.ERROR:
                a.down([msg])
                return True
            if t is MessageType.COMPLETE:
                a.down([msg])
                return True
            if index == 1 and t is MessageType.DATA:
                a.emit(src.get())
                return True
            if index == 1 and t is MessageType.RESOLVED:
                return True
            return index == 0

        return node(
            [src, notifier],
            compute,
            on_message=on_message,
            describe_kind="sample",
            complete_when_deps_complete=False,
        )

    return _op


def audit(seconds: float) -> PipeOperator:
    """Emit the latest upstream value after ``seconds`` of trailing silence (Rx ``auditTime``).

    Each ``DATA`` stores the latest value and restarts the timer. When the timer fires,
    the stored value is emitted. No leading-edge emission.

    Args:
        seconds: Trailing window duration in seconds.

    Returns:
        A unary pipe operator ``(Node) -> Node``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import audit
        src = state(0)
        out = pipe(src, audit(0.05))
        ```
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            timer: threading.Timer | None = None
            latest: Any = None
            has = False

            def cancel_timer() -> None:
                nonlocal timer
                if timer is not None:
                    timer.cancel()
                    timer = None

            def fire() -> None:
                nonlocal timer, has
                timer = None
                if has:
                    has = False
                    actions.emit(latest)

            def outer_sink(msgs: Messages) -> None:
                nonlocal timer, latest, has
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        latest = _msg_val(m)
                        has = True
                        cancel_timer()
                        tt = threading.Timer(seconds, fire)
                        tt.daemon = True
                        tt.start()
                        timer = tt
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
    """Delay each ``DATA`` message by ``seconds`` (one timer per pending value, FIFO order).

    Args:
        seconds: Delay in seconds applied to each ``DATA`` message.

    Returns:
        A unary pipe operator ``(Node) -> Node``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import delay
        src = state(0)
        out = pipe(src, delay(0.01))
        ```
    """

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
    """Emit ``ERROR`` if no ``DATA`` arrives within ``seconds`` after subscribe or last ``DATA``.

    Timer resets on each ``DATA``; unsubscribe cancels the watchdog.

    Args:
        seconds: Timeout window in seconds.
        error: Exception to send as the ``ERROR`` payload (default: :exc:`TimeoutError`).

    Returns:
        A unary pipe operator ``(Node) -> Node``.

    Example:
        ```python
        from graphrefly.extra.tier2 import timeout
        from graphrefly.extra import never
        from graphrefly.extra.sources import first_value_from
        n = timeout(0.001)(never())
        try:
            first_value_from(n)
        except Exception:
            pass
        ```
    """

    err = error if error is not None else TimeoutError("timeout")

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            timer: threading.Timer | None = None

            def cancel_timer() -> None:
                nonlocal timer
                if timer is not None:
                    timer.cancel()
                    timer = None

            def schedule() -> None:
                nonlocal timer
                cancel_timer()

                def fire() -> None:
                    nonlocal timer
                    timer = None
                    actions.down([(MessageType.ERROR, err)])

                tt = threading.Timer(seconds, fire)
                tt.daemon = True
                tt.start()
                timer = tt

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
    """Collect ``DATA`` values in a buffer; emit the list when ``notifier`` emits ``DATA``.

    Args:
        notifier: Node whose ``DATA`` flushes the accumulated buffer.

    Returns:
        A unary pipe operator ``(Node) -> Node[list]``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import buffer
        src = state(0)
        flush = state(None)
        out = pipe(src, buffer(flush))
        ```
    """

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
    """Emit a list of every ``n`` consecutive ``DATA`` values as a single emission.

    Args:
        n: Number of ``DATA`` values to collect per emitted list.

    Returns:
        A unary pipe operator ``(Node) -> Node[list]``.

    Example:
        ```python
        from graphrefly.extra import of
        from graphrefly.extra.tier2 import buffer_count
        from graphrefly.extra.sources import first_value_from
        out = buffer_count(3)(of(1, 2, 3, 4))
        assert first_value_from(out) == [1, 2, 3]
        ```
    """

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
    """Emit a list of ``DATA`` values collected over each timed window of ``seconds``.

    Args:
        seconds: Window duration in seconds.

    Returns:
        A unary pipe operator ``(Node) -> Node[list]``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import buffer_time
        src = state(0)
        out = pipe(src, buffer_time(0.1))
        ```
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            buf: list[Any] = []
            timer: threading.Timer | None = None

            def cancel() -> None:
                nonlocal timer
                if timer is not None:
                    timer.cancel()
                    timer = None

            def flush() -> None:
                nonlocal timer
                timer = None
                if buf:
                    actions.emit(list(buf))
                    buf.clear()

            def arm() -> None:
                nonlocal timer
                cancel()
                tt = threading.Timer(seconds, flush)
                tt.daemon = True
                tt.start()
                timer = tt

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
    """Create a producer that emits ``0, 1, 2, …`` at a fixed timer interval.

    Args:
        seconds: Interval between emissions in seconds.

    Returns:
        A :class:`~graphrefly.core.node.Node` that emits on a recurring timer thread.

    Example:
        ```python
        from graphrefly.extra.tier2 import interval
        from graphrefly.extra.sources import first_value_from
        n = interval(0.001)
        assert first_value_from(n) == 0
        ```
    """

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        n = 0
        timer: threading.Timer | None = None
        stopped = False

        def cancel() -> None:
            nonlocal timer
            if timer is not None:
                timer.cancel()
                timer = None

        def tick() -> None:
            nonlocal n, timer
            if stopped:
                return
            actions.emit(n)
            n += 1
            tt = threading.Timer(seconds, tick)
            tt.daemon = True
            tt.start()
            timer = tt

        tt0 = threading.Timer(seconds, tick)
        tt0.daemon = True
        tt0.start()
        timer = tt0

        def cleanup() -> None:
            nonlocal stopped
            stopped = True
            cancel()

        return cleanup

    # No ``initial``: first tick emits ``0``; matching ``initial=0`` would coalesce to RESOLVED.
    return node(start, describe_kind="interval", complete_when_deps_complete=False)


def repeat(times: int) -> PipeOperator:
    """Play the source to ``COMPLETE``, then re-subscribe, repeating ``times`` passes total.

    Each pass ends when the source emits ``COMPLETE``; the operator then
    subscribes again until all passes have finished.

    Args:
        times: Total number of source passes to play through.

    Returns:
        A unary pipe operator ``(Node) -> Node``.

    Example:
        ```python
        from graphrefly.extra import of
        from graphrefly.extra.tier2 import repeat
        from graphrefly.extra.sources import to_list
        assert to_list(repeat(3)(of(1))) == [1, 1, 1]
        ```
    """

    if times <= 0:
        msg = "repeat expects times > 0"
        raise ValueError(msg)

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            remaining = times
            outer_unsub: Callable[[], None] | None = None

            def detach() -> None:
                nonlocal outer_unsub
                if outer_unsub is not None:
                    outer_unsub()
                    outer_unsub = None

            def attach() -> None:
                nonlocal outer_unsub
                detach()

                def outer_sink(msgs: Messages) -> None:
                    nonlocal remaining
                    for m in msgs:
                        t = m[0]
                        if t is MessageType.DATA:
                            actions.emit(_msg_val(m))
                        elif t is MessageType.DIRTY:
                            actions.down([(MessageType.DIRTY,)])
                        elif t is MessageType.RESOLVED:
                            actions.down([(MessageType.RESOLVED,)])
                        elif t is MessageType.COMPLETE:
                            remaining -= 1
                            detach()
                            if remaining <= 0:
                                actions.down([(MessageType.COMPLETE,)])
                            else:
                                attach()
                        elif t is MessageType.ERROR:
                            detach()
                            actions.down([m])
                        else:
                            actions.down([m])

                outer_unsub = src.subscribe(outer_sink)

            attach()

            def cleanup() -> None:
                detach()

            return cleanup

        return node(start, describe_kind="repeat", complete_when_deps_complete=False)

    return _op


def gate(control: Node[Any]) -> PipeOperator:
    """Forward ``DATA`` only when ``control`` is truthy; otherwise emit ``RESOLVED``.

    This is a value-level gate using a boolean control signal. See :func:`pausable`
    for protocol-level ``PAUSE``/``RESUME`` buffering.

    Args:
        control: Boolean-valued node; ``True`` lets values through, ``False`` suppresses them.

    Returns:
        A unary pipe operator ``(Node) -> Node``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import gate
        src = state(1)
        ctrl = state(True)
        out = pipe(src, gate(ctrl))
        ```
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

    Returns:
        A unary pipe operator ``(Node) -> Node``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import pausable
        from graphrefly.core.protocol import MessageType
        src = state(0)
        out = pipe(src, pausable())
        out.down([(MessageType.PAUSE,)])
        out.down([(MessageType.RESUME,)])
        ```
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            paused = False
            backlog: list[Any] = []

            def outer_sink(msgs: Messages) -> None:
                nonlocal paused
                for m in msgs:
                    t = m[0]
                    if t is MessageType.PAUSE:
                        paused = True
                        actions.down([m])
                    elif t is MessageType.RESUME:
                        paused = False
                        actions.down([m])
                        for bm in backlog:
                            actions.down([bm])
                        backlog.clear()
                    elif paused and t in (
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
    """Turn upstream ``ERROR`` into a ``DATA`` value produced by ``recover(exc)``.

    Args:
        recover: Callable ``(exception) -> value`` whose return is emitted as ``DATA``.

    Returns:
        A unary pipe operator ``(Node) -> Node``.

    Example:
        ```python
        from graphrefly.extra import throw_error
        from graphrefly.extra.tier2 import rescue
        from graphrefly.extra.sources import first_value_from
        n = rescue(lambda e: -1)(throw_error(ValueError("oops")))
        assert first_value_from(n) == -1
        ```
    """

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
    """Split source ``DATA`` into sub-node windows; open a new window on each notifier ``DATA``.

    Each emitted value is a :class:`~graphrefly.core.node.Node` receiving the
    ``DATA`` values belonging to that window.

    Args:
        notifier: Node whose ``DATA`` opens a new window.

    Returns:
        A unary pipe operator ``(Node) -> Node[Node]``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import window
        src = state(0)
        tick = state(None)
        out = pipe(src, window(tick))
        ```
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            win_actions: NodeActions | None = None
            win_unsub: Callable[[], None] | None = None

            def close_win() -> None:
                nonlocal win_actions, win_unsub
                if win_actions is not None:
                    win_actions.down([(MessageType.COMPLETE,)])
                win_actions = None
                if win_unsub is not None:
                    win_unsub()
                    win_unsub = None

            def open_win() -> None:
                nonlocal win_actions, win_unsub
                holder: list[NodeActions | None] = [None]

                def win_start(_d: list[Any], wa: NodeActions) -> Callable[[], None]:
                    holder[0] = wa
                    return lambda: None

                w = node(win_start, describe_kind="window_inner", complete_when_deps_complete=False)
                win_unsub = w.subscribe(lambda _msgs: None)
                win_actions = holder[0]
                actions.emit(w)

            def src_sink(msgs: Messages) -> None:
                nonlocal win_actions
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        if win_actions is None:
                            open_win()
                        if win_actions is not None:
                            win_actions.down([(MessageType.DATA, _msg_val(m))])
                    elif t is MessageType.COMPLETE:
                        close_win()
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        if win_actions is not None:
                            win_actions.down([m])
                        win_actions = None
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
    """Split source ``DATA`` into sub-node windows of ``n`` items each.

    Args:
        n: Number of ``DATA`` values per sub-node window.

    Returns:
        A unary pipe operator ``(Node) -> Node[Node]``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import window_count
        src = state(0)
        out = pipe(src, window_count(3))
        ```
    """

    if n <= 0:
        msg = "window_count expects n > 0"
        raise ValueError(msg)

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            win_actions: NodeActions | None = None
            win_unsub: Callable[[], None] | None = None
            count = 0

            def close_win() -> None:
                nonlocal win_actions, win_unsub
                if win_actions is not None:
                    win_actions.down([(MessageType.COMPLETE,)])
                win_actions = None
                if win_unsub is not None:
                    win_unsub()
                    win_unsub = None

            def open_win() -> None:
                nonlocal win_actions, win_unsub, count
                holder: list[NodeActions | None] = [None]

                def win_start(_d: list[Any], wa: NodeActions) -> Callable[[], None]:
                    holder[0] = wa
                    return lambda: None

                w = node(win_start, describe_kind="window_inner", complete_when_deps_complete=False)
                win_unsub = w.subscribe(lambda _msgs: None)
                win_actions = holder[0]
                count = 0
                actions.emit(w)

            def outer_sink(msgs: Messages) -> None:
                nonlocal win_actions, count
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        if win_actions is None:
                            open_win()
                        if win_actions is not None:
                            win_actions.down([(MessageType.DATA, _msg_val(m))])
                        count += 1
                        if count >= n:
                            close_win()
                    elif t is MessageType.COMPLETE:
                        close_win()
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        if win_actions is not None:
                            win_actions.down([m])
                        win_actions = None
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
    """Split source ``DATA`` into sub-node windows, each lasting ``seconds``.

    Each emitted value is a :class:`~graphrefly.core.node.Node` receiving values
    collected during that time window.

    Args:
        seconds: Duration of each window in seconds.

    Returns:
        A unary pipe operator ``(Node) -> Node[Node]``.

    Example:
        ```python
        from graphrefly import state, pipe
        from graphrefly.extra.tier2 import window_time
        src = state(0)
        out = pipe(src, window_time(0.1))
        ```
    """

    def _op(src: Node[Any]) -> Node[Any]:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            win_actions: NodeActions | None = None
            win_unsub: Callable[[], None] | None = None
            timer: threading.Timer | None = None

            def close_win() -> None:
                nonlocal win_actions, win_unsub
                if win_actions is not None:
                    win_actions.down([(MessageType.COMPLETE,)])
                win_actions = None
                if win_unsub is not None:
                    win_unsub()
                    win_unsub = None

            def open_win() -> None:
                nonlocal win_actions, win_unsub
                holder: list[NodeActions | None] = [None]

                def win_start(_d: list[Any], wa: NodeActions) -> Callable[[], None]:
                    holder[0] = wa
                    return lambda: None

                w = node(win_start, describe_kind="window_inner", complete_when_deps_complete=False)
                win_unsub = w.subscribe(lambda _msgs: None)
                win_actions = holder[0]
                actions.emit(w)

            def rotate() -> None:
                close_win()
                open_win()
                arm()

            def arm() -> None:
                nonlocal timer
                if timer is not None:
                    timer.cancel()
                tt = threading.Timer(seconds, rotate)
                tt.daemon = True
                tt.start()
                timer = tt

            open_win()
            arm()

            def outer_sink(msgs: Messages) -> None:
                nonlocal timer, win_actions
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        if win_actions is not None:
                            win_actions.down([(MessageType.DATA, _msg_val(m))])
                    elif t is MessageType.COMPLETE:
                        if timer is not None:
                            timer.cancel()
                            timer = None
                        close_win()
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        if timer is not None:
                            timer.cancel()
                            timer = None
                        if win_actions is not None:
                            win_actions.down([m])
                        win_actions = None
                        actions.down([m])
                    elif t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    else:
                        actions.down([m])

            outer_unsub = src.subscribe(outer_sink)

            def cleanup() -> None:
                nonlocal timer
                if timer is not None:
                    timer.cancel()
                    timer = None
                close_win()
                outer_unsub()

            return cleanup

        return node(start, describe_kind="window_time", complete_when_deps_complete=False)

    return _op


debounce_time = debounce
throttle_time = throttle
catch_error = rescue
merge_map = flat_map

__all__ = [
    "audit",
    "buffer",
    "buffer_count",
    "buffer_time",
    "catch_error",
    "concat_map",
    "debounce",
    "debounce_time",
    "delay",
    "exhaust_map",
    "flat_map",
    "gate",
    "interval",
    "merge_map",
    "pausable",
    "repeat",
    "rescue",
    "sample",
    "switch_map",
    "throttle",
    "throttle_time",
    "timeout",
    "window",
    "window_count",
    "window_time",
]
