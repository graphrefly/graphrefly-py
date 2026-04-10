"""Sources, sinks, and multicast helpers from :func:`~graphrefly.core.node.node` (roadmap §2.3).

Cold sync sources use a no-deps producer (same pattern as :func:`~graphrefly.extra.tier2.interval`).
Multicast helpers are thin dependency wires so one upstream subscription is shared across all
downstream sinks of the returned node (ref-counted disconnect when the last sink unsubscribes).

Protocol/system/ingest adapters (HTTP, WebSocket, SSE, MCP, git, fs, Kafka, etc.) have moved to
:mod:`graphrefly.extra.adapters`.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from datetime import datetime
from typing import Any

from graphrefly.core.node import NO_VALUE, Node, NodeActions, node
from graphrefly.core.protocol import Messages, MessageType


def _source_initial_kwargs(source: Node[Any]) -> dict[str, Any]:
    """Return ``{"initial": value}`` when *source* has a real cached value, else ``{}``."""
    raw = getattr(source, "_cached", NO_VALUE)
    return {"initial": raw} if raw is not NO_VALUE else {}


def _msg_val(m: tuple[Any, ...]) -> Any:
    assert len(m) >= 2
    return m[1]


# --- static sources -----------------------------------------------------------


def of(*values: Any) -> Node[Any]:
    """Emit each argument as ``DATA`` in order, then ``COMPLETE`` on subscribe.

    Args:
        *values: Values to emit sequentially as ``DATA`` messages.

    Returns:
        A cold :class:`~graphrefly.core.node.Node` that completes after emitting all values.

    Example:
        ```python
        from graphrefly.extra import of
        from graphrefly.extra.sources import first_value_from
        n = of(1, 2, 3)
        assert first_value_from(n) == 1
        ```
    """

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
    """Emit ``COMPLETE`` immediately when the first sink subscribes.

    Returns:
        A :class:`~graphrefly.core.node.Node` that completes with no ``DATA``.

    Example:
        ```python
        from graphrefly.extra import empty
        from graphrefly.extra.sources import to_list
        assert to_list(empty()) == []
        ```
    """

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        actions.down([(MessageType.COMPLETE,)])
        return lambda: None

    return node(start, describe_kind="empty", complete_when_deps_complete=False)


def never() -> Node[Any]:
    """Create a source that never emits any messages.

    Returns:
        A :class:`~graphrefly.core.node.Node` whose producer is a no-op
        (no ``DATA``, no ``COMPLETE``).

    Example:
        ```python
        from graphrefly.extra import never
        n = never()
        # n.get() is None; no DATA will ever arrive
        ```
    """

    def start(_deps: list[Any], _actions: NodeActions) -> Callable[[], None]:
        return lambda: None

    return node(start, describe_kind="never", complete_when_deps_complete=False)


def throw_error(error: BaseException | Any) -> Node[Any]:
    """Emit a single ``ERROR`` message when the first sink subscribes.

    Args:
        error: The exception or value to send as the ``ERROR`` payload.

    Returns:
        A :class:`~graphrefly.core.node.Node` that immediately errors on subscribe.

    Example:
        ```python
        from graphrefly.extra import throw_error
        n = throw_error(ValueError("bad"))
        try:
            from graphrefly.extra.sources import first_value_from
            first_value_from(n)
        except ValueError:
            pass
        ```
    """

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        actions.down([(MessageType.ERROR, error)])
        return lambda: None

    return node(start, describe_kind="throw_error", complete_when_deps_complete=False)


# --- iterable / timer / cron --------------------------------------------------


def from_iter(iterable: Iterable[Any]) -> Node[Any]:
    """Drain a synchronous iterable on subscribe, emitting one ``DATA`` per item then ``COMPLETE``.

    If iteration raises an exception, the producer emits ``ERROR`` and stops.

    Args:
        iterable: Any synchronous iterable (list, generator, etc.).

    Returns:
        A cold :class:`~graphrefly.core.node.Node` that completes after the iterable is drained.

    Example:
        ```python
        from graphrefly.extra import from_iter
        from graphrefly.extra.sources import to_list
        assert to_list(from_iter([1, 2, 3])) == [1, 2, 3]
        ```
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
    """Emit a value after a delay, then optionally tick at a fixed period (like Rx ``timer``).

    If *period* is ``None``, emit *first* once then ``COMPLETE``. If *period* is
    set, emit *first*, *first+1*, *first+2*, ... every *period* seconds. Timer
    threads are daemonized and cancelled on unsubscribe.

    Args:
        delay: Seconds to wait before the first emission (must be >= 0).
        period: Optional repeat interval in seconds (``None`` = one-shot).
        first: Integer value for the first emission (default ``0``).

    Returns:
        A :class:`~graphrefly.core.node.Node` that emits on a timer thread.

    Example:
        ```python
        from graphrefly.extra import from_timer
        from graphrefly.extra.sources import first_value_from
        n = from_timer(0.001)
        assert first_value_from(n) == 0
        ```
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

    Emits wall-clock nanosecond timestamp on each match.
    Uses a built-in cron parser (no external dependencies).
    """
    from graphrefly.core.clock import wall_clock_ns as _wall_clock_ns
    from graphrefly.extra.cron import CronSchedule, matches_cron, parse_cron

    schedule: CronSchedule = parse_cron(expr)

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        timer: list[threading.Timer | None] = [None]
        stopped = [False]
        last_fired_key = [-1]

        def check() -> None:
            if stopped[0]:
                return
            now = datetime.fromtimestamp(_wall_clock_ns() / 1_000_000_000)
            key = (
                now.year * 100_000_000
                + now.month * 1_000_000
                + now.day * 10_000
                + now.hour * 100
                + now.minute
            )
            if key != last_fired_key[0] and matches_cron(schedule, now):
                last_fired_key[0] = key
                actions.emit(_wall_clock_ns())
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


def from_awaitable(
    awaitable: Awaitable[Any],
    *,
    runner: Any | None = None,
) -> Node[Any]:
    """Resolve an awaitable via a :class:`~graphrefly.core.runner.Runner`.

    Args:
        awaitable: The awaitable/coroutine to resolve.
        runner: Optional :class:`~graphrefly.core.runner.Runner`.  When ``None``,
            uses the thread-local default runner (see
            :func:`~graphrefly.core.runner.set_default_runner`).

    Returns:
        A :class:`~graphrefly.core.node.Node` that emits one ``DATA`` then
        ``COMPLETE``, or ``ERROR`` on failure.
    """
    from graphrefly.core.runner import resolve_runner

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        stopped = [False]

        async def arun() -> Any:
            return await awaitable

        def on_result(v: Any) -> None:
            if not stopped[0]:
                actions.emit(v)
                actions.down([(MessageType.COMPLETE,)])

        def on_error(err: BaseException) -> None:
            if not stopped[0]:
                actions.down([(MessageType.ERROR, err)])

        cancel = resolve_runner(runner).schedule(arun(), on_result, on_error)

        def cleanup() -> None:
            stopped[0] = True
            cancel()

        return cleanup

    return node(start, describe_kind="from_awaitable", complete_when_deps_complete=False)


def from_async_iter(
    aiterable: AsyncIterable[Any],
    *,
    runner: Any | None = None,
) -> Node[Any]:
    """Iterate an async iterable via a :class:`~graphrefly.core.runner.Runner`.

    Args:
        aiterable: The async iterable to drain.
        runner: Optional :class:`~graphrefly.core.runner.Runner`.  When ``None``,
            uses the thread-local default runner.

    Returns:
        A :class:`~graphrefly.core.node.Node` that emits ``DATA`` per item,
        then ``COMPLETE``, or ``ERROR`` on failure.
    """
    from graphrefly.core.runner import resolve_runner

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        stopped = [False]

        async def arun() -> None:
            async for item in aiterable:
                if stopped[0]:
                    return
                actions.emit(item)

        def on_result(_: Any) -> None:
            if not stopped[0]:
                actions.down([(MessageType.COMPLETE,)])

        def on_error(err: BaseException) -> None:
            if not stopped[0]:
                actions.down([(MessageType.ERROR, err)])

        cancel = resolve_runner(runner).schedule(arun(), on_result, on_error)

        def cleanup() -> None:
            stopped[0] = True
            cancel()

        return cleanup

    return node(start, describe_kind="from_async_iter", complete_when_deps_complete=False)


def from_any(value: Any, *, runner: Any | None = None) -> Node[Any]:
    """Coerce a value into a :class:`~graphrefly.core.node.Node` using the best matching source.

    Dispatch rules:

    - Existing :class:`~graphrefly.core.node.Node` -> returned as-is.
    - :class:`collections.abc.AsyncIterable` / async iterator -> :func:`from_async_iter`.
    - :class:`collections.abc.Awaitable` (incl. coroutines, futures) -> :func:`from_awaitable`.
    - Otherwise tries ``iter(value)``; if that fails uses :func:`of`.

    Args:
        value: Any value to coerce.
        runner: Optional :class:`~graphrefly.core.runner.Runner` forwarded to
            :func:`from_awaitable` / :func:`from_async_iter` when applicable.

    Returns:
        A :class:`~graphrefly.core.node.Node` wrapping *value*.

    Example:
        ```python
        from graphrefly.extra.sources import from_any
        n = from_any([1, 2, 3])
        from graphrefly.extra.sources import to_list
        assert to_list(n) == [1, 2, 3]
        ```
    """
    if isinstance(value, Node):
        return value
    if isinstance(value, AsyncIterable):
        return from_async_iter(value, runner=runner)
    if isinstance(value, Awaitable):
        return from_awaitable(value, runner=runner)
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
    """Subscribe to *source* and invoke ``fn(value)`` for each ``DATA`` message.

    Args:
        source: The node to subscribe to.
        fn: Callback invoked with each ``DATA`` payload.
        on_error: Optional callback invoked when an ``ERROR`` is received. If
            omitted, the error is re-raised from inside the sink.

    Returns:
        An unsubscribe callable; call it to detach.

    Example:
        ```python
        from graphrefly import state
        from graphrefly.extra.sources import for_each
        x = state(0)
        log = []
        unsub = for_each(x, log.append)
        x.down([("DATA", 7)])
        unsub()
        assert log == [7]
        ```
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
    """Block until ``COMPLETE`` or ``ERROR``, collecting all ``DATA`` payloads in order.

    Args:
        source: The node to collect from.
        timeout: Optional timeout in seconds; raises :exc:`TimeoutError` if
            ``COMPLETE`` does not arrive in time.

    Returns:
        A list of ``DATA`` payloads in emission order.

    Example:
        ```python
        from graphrefly.extra import of
        from graphrefly.extra.sources import to_list
        assert to_list(of(1, 2, 3)) == [1, 2, 3]
        ```
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
    """Block until the first ``DATA`` value or a terminal ``ERROR`` arrives.

    On ``COMPLETE`` without prior ``DATA``, raises :exc:`StopIteration`. With
    *timeout*, raises :exc:`TimeoutError` if no terminal message arrives in time.

    **Important:** This subscribes to *source* and waits for a **future**
    emission. It does NOT read the cached value — data that has already
    flowed is gone. You must call this **before** the upstream emits, or
    use ``source.get()`` / ``source.status`` to read already-cached state.
    See COMPOSITION-GUIDE §2 (subscription ordering).

    Args:
        source: The node to await the first value from.
        timeout: Optional timeout in seconds.

    Returns:
        The first ``DATA`` payload received.

    Notes:
        Python exposes this as a synchronous blocking call. The TypeScript equivalent
        ``firstValueFrom`` returns a ``Promise``; both provide the same escape-hatch
        semantics with implementation differences due to language concurrency models.

    Example:
        ```python
        from graphrefly.extra import of
        from graphrefly.extra.sources import first_value_from
        assert first_value_from(of(42)) == 42
        ```
    """
    got: list[Any] = [NO_VALUE]
    err_box: list[BaseException | Any | None] = [None]
    complete_without_data = [False]
    done = threading.Event()

    def sink(msgs: Messages) -> None:
        for m in msgs:
            t = m[0]
            if t is MessageType.DATA:
                if got[0] is NO_VALUE:
                    got[0] = _msg_val(m)
                done.set()
            elif t is MessageType.ERROR:
                err_box[0] = _msg_val(m)
                done.set()
            elif t is MessageType.COMPLETE:
                if got[0] is NO_VALUE:
                    complete_without_data[0] = True
                done.set()

    unsub = source.subscribe(sink)
    try:
        # If the source is already terminal (errored/completed) the subscribe
        # handshake delivers nothing (spec §2.2). Check status immediately so
        # we don't block on a node that will never emit again.
        if not done.is_set():
            status = getattr(source, "status", None)
            if status == "errored":
                if err_box[0] is None:
                    err_box[0] = RuntimeError("source node is in errored state")
                done.set()
            elif status == "completed":
                if got[0] is NO_VALUE:
                    complete_without_data[0] = True
                done.set()

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
    if complete_without_data[0] and got[0] is NO_VALUE:
        raise StopIteration
    return got[0]


def first_where(
    source: Node[Any],
    predicate: Callable[[Any], bool],
    *,
    timeout: float | None = None,
) -> Any:
    """Block until the first ``DATA`` value satisfying *predicate* arrives.

    Subscribes directly and checks *predicate* on each ``DATA`` emission.
    No polling. Use in tests and bridging code where you need to wait for
    a specific value synchronously.

    **Important:** This only captures **future** emissions — data that has
    already flowed through the node is gone and will not be seen. You must
    call this **before** the upstream emits. For already-cached values, use
    ``source.get()`` / ``source.status`` instead. See COMPOSITION-GUIDE §2.

    Args:
        source: The node to observe.
        predicate: Called with each DATA value; returns ``True`` to accept.
        timeout: Optional timeout in seconds.

    Returns:
        The first DATA payload where ``predicate(value)`` is ``True``.

    Example:
        ```python
        val = first_where(strategy.node, lambda snap: len(snap) > 0, timeout=5.0)
        ```
    """
    got: list[Any | None] = [None]
    err_box: list[BaseException | Any | None] = [None]
    done = threading.Event()

    def sink(msgs: Messages) -> None:
        for m in msgs:
            t = m[0]
            if t is MessageType.DATA:
                v = _msg_val(m)
                if got[0] is None and predicate(v):
                    got[0] = v
                    done.set()
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
            msg = "first_where timed out"
            raise TimeoutError(msg)
    finally:
        unsub()

    err = err_box[0]
    if err is not None:
        if isinstance(err, BaseException):
            raise err
        raise RuntimeError(str(err))
    if got[0] is None:
        msg = "first_where: source completed without a matching value"
        raise StopIteration(msg)
    return got[0]


# --- multicast ----------------------------------------------------------------


def share[T](source: Node[T]) -> Node[T]:
    """Share one upstream subscription across all downstream sinks (ref-counted).

    Args:
        source: The upstream node to multicast.

    Returns:
        A new :class:`~graphrefly.core.node.Node` that connects to *source* once
        and ref-counts downstream subscriptions.

    Example:
        ```python
        from graphrefly import state
        from graphrefly.extra.sources import share, for_each
        x = state(0)
        s = share(x)
        log = []
        unsub = for_each(s, log.append)
        x.down([("DATA", 1)])
        unsub()
        ```
    """
    return node([source], describe_kind="share", **_source_initial_kwargs(source))


def cached[T](source: Node[T]) -> Node[T]:
    """Alias of :func:`share` with ``describe_kind='cached'`` (hot wire).

    Late joiners observe new ``DATA`` from the shared upstream; use
    :meth:`~graphrefly.core.node.Node.get` after subscribe for the latest cached value on the
    returned node.
    """
    return node([source], describe_kind="cached", **_source_initial_kwargs(source))


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
        **_kw: Any,
    ) -> Callable[[], None]:
        # Replay buffered values before connecting live stream
        for v in list(self._replay_buf):
            sink([(MessageType.DATA, v)])
        return super().subscribe(sink, hints, actor=actor)


def replay[T](source: Node[T], buffer_size: int = 1) -> Node[T]:
    """Multicast with late-subscriber replay of the last *buffer_size* ``DATA`` payloads.

    Args:
        source: The upstream node to multicast.
        buffer_size: Number of ``DATA`` payloads to buffer for late joiners (>= 1).

    Returns:
        A :class:`~graphrefly.core.node.Node` that replays buffered values to
        each new subscriber before connecting the live stream.

    Example:
        ```python
        from graphrefly import state
        from graphrefly.extra.sources import replay, for_each
        x = state(0)
        r = replay(x, buffer_size=2)
        x.down([("DATA", 1)])
        x.down([("DATA", 2)])
        received = []
        unsub = for_each(r, received.append)
        # received includes replayed values 1 and 2
        unsub()
        ```
    """
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


share_replay = replay


__all__ = [
    "cached",
    "empty",
    "first_value_from",
    "for_each",
    "from_any",
    "from_async_iter",
    "from_awaitable",
    "from_cron",
    "from_iter",
    "from_timer",
    "never",
    "of",
    "replay",
    "share",
    "share_replay",
    "throw_error",
    "to_array",
    "to_list",
]
