"""Sources, sinks, and multicast helpers from :func:`~graphrefly.core.node.node` (roadmap §2.3).

Cold sync sources use a no-deps producer (same pattern as :func:`~graphrefly.extra.tier2.interval`).
Multicast helpers are thin dependency wires so one upstream subscription is shared across all
downstream sinks of the returned node (ref-counted disconnect when the last sink unsubscribes).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from concurrent.futures import Future
from contextlib import suppress
from datetime import datetime
from typing import Any, cast

from graphrefly.core.node import Node, NodeActions, node
from graphrefly.core.protocol import Messages, MessageType


def _msg_val(m: tuple[Any, ...]) -> Any:
    assert len(m) >= 2
    return m[1]


# --- static sources -----------------------------------------------------------


def of(*values: Any) -> Node[Any]:
    """Emit each argument as ``DATA`` in order, then ``COMPLETE`` (cold on subscribe)."""

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        try:
            for v in values:
                actions.emit(v)
            actions.down([(MessageType.COMPLETE,)])
        except BaseException as err:
            actions.down([(MessageType.ERROR, err)])
        return lambda: None

    return node(start, describe_kind="of", complete_when_deps_complete=False)


def empty() -> Node[Any]:
    """Emit ``COMPLETE`` immediately when the first sink subscribes."""

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        actions.down([(MessageType.COMPLETE,)])
        return lambda: None

    return node(start, describe_kind="empty", complete_when_deps_complete=False)


def never() -> Node[Any]:
    """Subscribe connects a no-op producer; no ``DATA`` or ``COMPLETE``."""

    def start(_deps: list[Any], _actions: NodeActions) -> Callable[[], None]:
        return lambda: None

    return node(start, describe_kind="never", complete_when_deps_complete=False)


def throw_error(error: BaseException | Any) -> Node[Any]:
    """Emit a single ``ERROR`` with *error* when the first sink subscribes."""

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        actions.down([(MessageType.ERROR, error)])
        return lambda: None

    return node(start, describe_kind="throw_error", complete_when_deps_complete=False)


# --- iterable / timer / cron --------------------------------------------------


def from_iter(iterable: Iterable[Any]) -> Node[Any]:
    """Drain a synchronous *iterable* on subscribe: one ``DATA`` per item, then ``COMPLETE``.

    If iteration raises, the producer emits ``ERROR`` and stops.
    """

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        try:
            for item in iterable:
                actions.emit(item)
            actions.down([(MessageType.COMPLETE,)])
        except BaseException as err:
            actions.down([(MessageType.ERROR, err)])
        return lambda: None

    return node(start, describe_kind="from_iter", complete_when_deps_complete=False)


def from_timer(
    delay: float,
    period: float | None = None,
    *,
    first: int = 0,
) -> Node[Any]:
    """Like Rx ``timer``: after *delay* seconds emit *first*, then optionally tick forever.

    If *period* is ``None``, emit once (value *first*) and ``COMPLETE``.
    If *period* is set, emit *first*, then *first+1*, *first+2*, … every *period* seconds
    (same counter shape as :func:`~graphrefly.extra.tier2.interval`).
    """

    if delay < 0 or (period is not None and period < 0):
        msg = "delay and period must be non-negative"
        raise ValueError(msg)

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        timer: list[threading.Timer | None] = [None]
        stopped = [False]
        n = [first]
        per = period

        def cancel() -> None:
            if timer[0] is not None:
                timer[0].cancel()
                timer[0] = None

        def tick_repeat() -> None:
            if stopped[0]:
                return
            assert per is not None
            actions.emit(n[0])
            n[0] += 1
            tt = threading.Timer(per, tick_repeat)
            tt.daemon = True
            tt.start()
            timer[0] = tt

        def after_delay() -> None:
            if stopped[0]:
                return
            if period is None:
                actions.emit(n[0])
                actions.down([(MessageType.COMPLETE,)])
                timer[0] = None
                return
            tick_repeat()

        tt0 = threading.Timer(delay, after_delay)
        tt0.daemon = True
        tt0.start()
        timer[0] = tt0

        def cleanup() -> None:
            stopped[0] = True
            cancel()

        return cleanup

    return node(start, describe_kind="from_timer", complete_when_deps_complete=False)


def from_cron(expr: str, *, tick_s: float = 60.0) -> Node[Any]:
    """Fire on each wall-clock minute matching a 5-field cron expression.

    Emits ``time.time_ns()`` (nanosecond timestamp) on each match.
    Uses a built-in cron parser (no external dependencies).
    """
    import time as _time

    from graphrefly.extra.cron import CronSchedule, matches_cron, parse_cron

    schedule: CronSchedule = parse_cron(expr)

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        timer: list[threading.Timer | None] = [None]
        stopped = [False]
        last_fired_key = [-1]

        def check() -> None:
            if stopped[0]:
                return
            now = datetime.now()
            key = (
                now.year * 100_000_000
                + now.month * 1_000_000
                + now.day * 10_000
                + now.hour * 100
                + now.minute
            )
            if key != last_fired_key[0] and matches_cron(schedule, now):
                last_fired_key[0] = key
                actions.emit(_time.time_ns())
            # Schedule next check
            if not stopped[0]:
                t = threading.Timer(tick_s, check)
                t.daemon = True
                t.start()
                timer[0] = t

        check()

        def cleanup() -> None:
            stopped[0] = True
            if timer[0] is not None:
                timer[0].cancel()
                timer[0] = None

        return cleanup

    return node(start, describe_kind="from_cron", complete_when_deps_complete=False)


# --- async bridges ------------------------------------------------------------


def from_awaitable(awaitable: Awaitable[Any]) -> Node[Any]:
    """Resolve an awaitable on a worker thread; one ``DATA`` then ``COMPLETE``, or ``ERROR``."""

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        stopped = [False]

        def run() -> None:
            async def arun() -> None:
                try:
                    v = await awaitable
                except BaseException as err:
                    if not stopped[0]:
                        actions.down([(MessageType.ERROR, err)])
                    return
                if not stopped[0]:
                    actions.emit(v)
                    actions.down([(MessageType.COMPLETE,)])

            try:
                asyncio.run(arun())
            except RuntimeError as run_err:
                # Only recover from nested ``asyncio.run`` / running-loop conflicts.
                msg = str(run_err).lower()
                if "asyncio.run()" not in msg and "running event loop" not in msg:
                    raise
                loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(arun())
                finally:
                    loop.close()
                    asyncio.set_event_loop(None)

        t = threading.Thread(target=run, daemon=True)
        t.start()

        def cleanup() -> None:
            stopped[0] = True

        return cleanup

    return node(start, describe_kind="from_awaitable", complete_when_deps_complete=False)


def from_async_iter(aiterable: AsyncIterable[Any]) -> Node[Any]:
    """Iterate an async iterable on a worker thread; ``DATA`` per item, then ``COMPLETE``."""

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        stopped = [False]

        def run() -> None:
            async def arun() -> None:
                try:
                    async for item in aiterable:
                        if stopped[0]:
                            return
                        actions.emit(item)
                    if not stopped[0]:
                        actions.down([(MessageType.COMPLETE,)])
                except BaseException as err:
                    if not stopped[0]:
                        actions.down([(MessageType.ERROR, err)])

            try:
                asyncio.run(arun())
            except RuntimeError as run_err:
                msg = str(run_err).lower()
                if "asyncio.run()" not in msg and "running event loop" not in msg:
                    raise
                loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(arun())
                finally:
                    loop.close()
                    asyncio.set_event_loop(None)

        t = threading.Thread(target=run, daemon=True)
        t.start()

        def cleanup() -> None:
            stopped[0] = True

        return cleanup

    return node(start, describe_kind="from_async_iter", complete_when_deps_complete=False)


def from_any(value: Any) -> Node[Any]:
    """Coerce *value* into a single-root :class:`~graphrefly.core.node.Node`.

    - Existing :class:`~graphrefly.core.node.Node` → returned as-is
    - :class:`collections.abc.AsyncIterable` / async iterator → :func:`from_async_iter`
    - Awaitable / :class:`asyncio.Future` / coroutine → :func:`from_awaitable`
    - Otherwise → :func:`from_iter` if ``iter(value)`` works (including ``str``), else :func:`of`
    """
    if isinstance(value, Node):
        return value
    if asyncio.isfuture(value) or asyncio.iscoroutine(value):
        return from_awaitable(cast("Awaitable[Any]", value))
    if isinstance(value, AsyncIterable):
        return from_async_iter(value)
    try:
        it = iter(value)
    except TypeError:
        return of(value)
    return from_iter(it)


# --- sinks --------------------------------------------------------------------


def for_each(
    source: Node[Any],
    fn: Callable[[Any], None],
    *,
    on_error: Callable[[BaseException], None] | None = None,
) -> Callable[[], None]:
    """Subscribe to *source* and invoke ``fn(value)`` for each ``DATA``.

    Returns an unsubscribe callable. Prefer ``on_error`` for ``ERROR`` handling; the default
    path raises the error from inside the sink and may not always propagate through
    :meth:`~graphrefly.core.node.Node.subscribe` when ``thread_safe`` is enabled.
    """

    def sink(msgs: Messages) -> None:
        for m in msgs:
            t = m[0]
            if t is MessageType.DATA:
                fn(_msg_val(m))
            elif t is MessageType.ERROR:
                err = _msg_val(m)
                if on_error is not None:
                    if isinstance(err, BaseException):
                        on_error(err)
                    else:
                        on_error(RuntimeError(str(err)))
                else:
                    if isinstance(err, BaseException):
                        raise err
                    msg = str(err)
                    raise RuntimeError(msg)

    return source.subscribe(sink)


def to_list(
    source: Node[Any],
    *,
    timeout: float | None = None,
) -> list[Any]:
    """Block until ``COMPLETE`` or ``ERROR``, collecting ``DATA`` payloads in order.

    Uses an internal subscribe + :class:`threading.Event`. On ``ERROR``, raises the error
    payload if it is a :class:`BaseException`, otherwise ``RuntimeError``.
    """
    out: list[Any] = []
    done = threading.Event()
    err_box: list[BaseException | Any | None] = [None]

    def sink(msgs: Messages) -> None:
        for m in msgs:
            t = m[0]
            if t is MessageType.DATA:
                out.append(_msg_val(m))
            elif t is MessageType.ERROR:
                err_box[0] = _msg_val(m)
                done.set()
            elif t is MessageType.COMPLETE:
                done.set()

    unsub = source.subscribe(sink)
    try:
        if timeout is None:
            done.wait()
        elif not done.wait(timeout):
            msg = "to_list timed out"
            raise TimeoutError(msg)
    finally:
        unsub()

    err = err_box[0]
    if err is not None:
        if isinstance(err, BaseException):
            raise err
        raise RuntimeError(str(err))
    return out


def first_value_from(
    source: Node[Any],
    *,
    timeout: float | None = None,
) -> Any:
    """The synchronous bridge: block until the first ``DATA`` or terminal ``ERROR``.

    On ``COMPLETE`` without prior ``DATA``, raises :class:`StopIteration`. With *timeout*,
    raises :class:`TimeoutError` if no terminal message arrives in time.
    """
    got: list[Any | None] = [None]
    err_box: list[BaseException | Any | None] = [None]
    complete_without_data = [False]
    done = threading.Event()

    def sink(msgs: Messages) -> None:
        for m in msgs:
            t = m[0]
            if t is MessageType.DATA:
                if got[0] is None:
                    got[0] = _msg_val(m)
                done.set()
            elif t is MessageType.ERROR:
                err_box[0] = _msg_val(m)
                done.set()
            elif t is MessageType.COMPLETE:
                if got[0] is None:
                    complete_without_data[0] = True
                done.set()

    unsub = source.subscribe(sink)
    try:
        if timeout is None:
            done.wait()
        elif not done.wait(timeout):
            msg = "first_value_from timed out"
            raise TimeoutError(msg)
    finally:
        unsub()

    err = err_box[0]
    if err is not None:
        if isinstance(err, BaseException):
            raise err
        raise RuntimeError(str(err))
    if complete_without_data[0] and got[0] is None:
        raise StopIteration
    return got[0]


def first_value_from_future(source: Node[Any]) -> Future[Any]:
    """Non-blocking bridge: return a :class:`concurrent.futures.Future` completed by first ``DATA``.

    The future fails with the ``ERROR`` payload, or :class:`LookupError` if the source completes
    without ``DATA``. Unsubscribes from *source* when the future completes.

    There is no built-in timeout: sources like :func:`never` leave the future pending until
    cancelled (``future.cancel()``). For blocking use with a deadline, prefer
    :func:`first_value_from` with *timeout*.
    """

    fut: Future[Any] = Future()
    unsub: list[Callable[[], None] | None] = [None]

    def finish() -> None:
        u = unsub[0]
        if u is not None:
            u()
            unsub[0] = None

    def sink(msgs: Messages) -> None:
        if fut.done():
            return
        for m in msgs:
            t = m[0]
            if t is MessageType.DATA:
                fut.set_result(_msg_val(m))
                finish()
                return
            if t is MessageType.ERROR:
                err = _msg_val(m)
                fut.set_exception(err if isinstance(err, BaseException) else RuntimeError(str(err)))
                finish()
                return
            if t is MessageType.COMPLETE:
                fut.set_exception(LookupError("completed without DATA"))
                finish()
                return

    unsub[0] = source.subscribe(sink)
    return fut


# --- multicast ----------------------------------------------------------------


def share[T](source: Node[T]) -> Node[T]:
    """Share one upstream subscription across all sinks of the returned node (ref-counted)."""
    return node([source], describe_kind="share", initial=source.get())


def cached[T](source: Node[T]) -> Node[T]:
    """Alias of :func:`share` with ``describe_kind='cached'`` (hot wire).

    Late joiners observe new ``DATA`` from the shared upstream; use
    :meth:`~graphrefly.core.node.Node.get` after subscribe for the latest cached value on the
    returned node.
    """
    return node([source], describe_kind="cached", initial=source.get())


class _ReplayNode[T](Node[T]):
    """Thin subclass that intercepts subscribe to replay buffered DATA to late joiners."""

    __slots__ = ("_replay_buf", "_replay_buf_size")

    def __init__(
        self,
        deps: list[Any],
        fn: Any,
        opts: dict[str, Any],
        buf: list[Any],
        buf_size: int,
    ) -> None:
        super().__init__(deps, fn, opts)
        self._replay_buf = buf
        self._replay_buf_size = buf_size

    def subscribe(
        self,
        sink: Callable[[Messages], None],
        hints: Any = None,
        *,
        actor: Any = None,
    ) -> Callable[[], None]:
        # Replay buffered values before connecting live stream
        for v in list(self._replay_buf):
            sink([(MessageType.DATA, v)])
        return super().subscribe(sink, hints, actor=actor)


def replay[T](source: Node[T], buffer_size: int = 1) -> Node[T]:
    """Multicast with late-subscriber replay of last *buffer_size* DATA payloads."""
    if buffer_size < 1:
        msg = "buffer_size must be >= 1"
        raise ValueError(msg)
    buf: list[Any] = []

    def on_msg(msg: tuple[Any, ...], _dep_index: int, actions: NodeActions) -> bool:
        if msg[0] is MessageType.DATA:
            val = msg[1]
            buf.append(val)
            if len(buf) > buffer_size:
                buf.pop(0)
        return False  # let default dispatch handle it

    opts: dict[str, Any] = {
        "on_message": on_msg,
        "describe_kind": "replay",
        "initial": source.get(),
    }
    return _ReplayNode([source], None, opts, buf, buffer_size)


def from_event_emitter(
    emitter: Any,
    event_name: str,
    *,
    add_method: str = "add_listener",
    remove_method: str = "remove_listener",
) -> Node[Any]:
    """Subscribe to an event emitter (e.g. custom emitter).

    Emits each event payload as DATA. Teardown removes the listener.
    Compatible with any object that has add/remove listener methods.
    """

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        def handler(*args: Any) -> None:
            if len(args) == 1:
                actions.emit(args[0])
            else:
                actions.emit(args)

        getattr(emitter, add_method)(event_name, handler)

        def cleanup() -> None:
            with suppress(Exception):
                getattr(emitter, remove_method)(event_name, handler)

        return cleanup

    return node(start, describe_kind="from_event_emitter", complete_when_deps_complete=False)


def to_array(source: Node[Any]) -> Node[list[Any]]:
    """Collect all DATA values; on COMPLETE emit one DATA (the list) then COMPLETE.

    Reactive version -- returns a Node. For blocking sync bridge, use :func:`to_list`.
    """
    acc: list[Any] = []

    def on_msg(msg: tuple[Any, ...], _dep_index: int, actions: NodeActions) -> bool:
        if msg[0] is MessageType.DATA:
            acc.append(msg[1] if len(msg) > 1 else None)
            return True
        if msg[0] is MessageType.COMPLETE:
            actions.emit(list(acc))
            actions.down([(MessageType.COMPLETE,)])
            return True
        return False

    return node(
        [source],
        on_message=on_msg,
        describe_kind="to_array",
        complete_when_deps_complete=False,
    )


__all__ = [
    "cached",
    "empty",
    "first_value_from",
    "first_value_from_future",
    "for_each",
    "from_any",
    "from_async_iter",
    "from_awaitable",
    "from_cron",
    "from_event_emitter",
    "from_iter",
    "from_timer",
    "never",
    "of",
    "replay",
    "share",
    "throw_error",
    "to_array",
    "to_list",
]
