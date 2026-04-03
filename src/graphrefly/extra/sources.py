"""Sources, sinks, and multicast helpers from :func:`~graphrefly.core.node.node` (roadmap §2.3).

Cold sync sources use a no-deps producer (same pattern as :func:`~graphrefly.extra.tier2.interval`).
Multicast helpers are thin dependency wires so one upstream subscription is shared across all
downstream sinks of the returned node (ref-counted disconnect when the last sink unsubscribes).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import urllib.error
import urllib.request
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from graphrefly.core.clock import wall_clock_ns
from graphrefly.core.node import Node, NodeActions, node
from graphrefly.core.protocol import Messages, MessageType, batch
from graphrefly.extra.resilience import WithStatusBundle, with_status


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
    set, emit *first*, *first+1*, *first+2*, … every *period* seconds. Timer
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

    - Existing :class:`~graphrefly.core.node.Node` → returned as-is.
    - :class:`collections.abc.AsyncIterable` / async iterator → :func:`from_async_iter`.
    - Awaitable / :class:`asyncio.Future` / coroutine → :func:`from_awaitable`.
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
    if isinstance(value, Awaitable) or asyncio.isfuture(value) or asyncio.iscoroutine(value):
        return from_awaitable(value, runner=runner)
    try:
        it = iter(value)
    except TypeError:
        return of(value)
    return from_iter(it)


@dataclass(frozen=True, slots=True)
class HttpBundle(WithStatusBundle):
    """Result of :func:`from_http`: pass-through value plus status companions."""

    fetch_count: Node[int]
    last_updated: Node[int]


def from_http(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: Any = None,
    transform: Callable[[Any], Any] | None = None,
    timeout_ns: int = 30_000_000_000,
    **kwargs: Any,
) -> HttpBundle:
    """Create a one-shot reactive HTTP source with lifecycle tracking.

    Uses :func:`urllib.request.urlopen` internally to remain zero-dependency.
    Performs a single fetch when subscribed, then completes. For periodic
    fetching, compose with ``switch_map`` and a time source.

    Args:
        url: The URL to fetch.
        method: HTTP method (default ``"GET"``).
        headers: Optional request headers.
        body: Optional request body (converted to JSON if not a string).
        transform: Optional function to transform raw response bytes
            (signature: ``Callable[[bytes], Any]``). Default: ``json.loads``.
        timeout_ns: Request timeout in **nanoseconds** (default ``30s``).
        **kwargs: Passed to :func:`~graphrefly.core.node.node` as options.

    Returns:
        An :class:`HttpBundle` wrapping the primary node and companions.

    Example:
        ```python
        from graphrefly.extra.sources import from_http
        from graphrefly.extra.tier2 import switch_map
        from graphrefly.extra import from_timer

        # One-shot:
        api = from_http("https://api.example.com/data")

        # Periodic polling via reactive composition:
        polled = switch_map(lambda _: from_http(url))(from_timer(0, period=5.0))
        ```
    Notes:
        This source is implemented with ``threading.Thread`` + ``urllib`` and does
        not currently support external cancellation signals (TS ``AbortSignal`` parity
        is deferred). Unsubscribe prevents any late emissions from being forwarded.
    """
    from graphrefly.core.sugar import state

    ns_per_sec = 1_000_000_000
    fetch_count = state(0, name=f"{kwargs.get('name', 'http')}/fetch_count")
    last_updated = state(0, name=f"{kwargs.get('name', 'http')}/last_updated")

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        active = [True]

        def task() -> None:
            if not active[0]:
                return
            try:
                data_bytes = None
                if body is not None:
                    if isinstance(body, str):
                        data_bytes = body.encode("utf-8")
                    else:
                        data_bytes = json.dumps(body).encode("utf-8")

                req = urllib.request.Request(url, data=data_bytes, method=method)
                if headers:
                    for k, v in headers.items():
                        req.add_header(k, v)

                with urllib.request.urlopen(req, timeout=timeout_ns / ns_per_sec) as response:
                    if not active[0]:
                        return
                    raw_data = response.read()
                    res_data = transform(raw_data) if transform else json.loads(raw_data)

                    if not active[0]:
                        return

                    with batch():
                        current_count = fetch_count.get()
                        next_count = (current_count if isinstance(current_count, int) else 0) + 1
                        fetch_count.down([(MessageType.DATA, next_count)])
                        last_updated.down([(MessageType.DATA, wall_clock_ns())])
                        actions.emit(res_data)
                    actions.down([(MessageType.COMPLETE,)])

            except BaseException as err:
                if not active[0]:
                    return
                actions.down([(MessageType.ERROR, err)])

        t = threading.Thread(target=task, daemon=True)
        t.start()

        def cleanup() -> None:
            active[0] = False

        return cleanup

    out = node(
        start,
        describe_kind="http",
        complete_when_deps_complete=False,
        **kwargs,
    )
    tracked = with_status(out)

    return HttpBundle(
        node=tracked.node,
        status=tracked.status,
        error=tracked.error,
        fetch_count=fetch_count,
        last_updated=last_updated,
    )


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


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = ["^"]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        out.append(re.escape(ch))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def _matches_any(path: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(path) is not None for p in patterns)


def _build_watchdog_backend(
    paths: list[str],
    recursive: bool,
    on_event: Callable[[str, str, str, str | None, str | None], None],
    on_error: Callable[[BaseException], None],
) -> tuple[list[Any], Callable[[], None]]:
    try:
        from watchdog.events import FileSystemEventHandler  # type: ignore[import-not-found]
        from watchdog.observers import Observer  # type: ignore[import-not-found]
    except Exception as err:  # pragma: no cover - exercised via monkeypatch in tests
        msg = (
            "from_fs_watch requires watchdog (no polling fallback by design). "
            "Install with `uv add watchdog`."
        )
        raise RuntimeError(msg) from err

    class _Handler(FileSystemEventHandler):  # type: ignore[misc]
        def __init__(self, root: str) -> None:
            super().__init__()
            self._root = root

        def on_any_event(self, event: Any) -> None:
            if getattr(event, "is_directory", False):
                return
            try:
                event_type = str(getattr(event, "event_type", "change"))
                src_path = getattr(event, "src_path", None)
                dest_path = getattr(event, "dest_path", None)
                path = str(dest_path or src_path or getattr(event, "path", ""))
                if path:
                    on_event(
                        event_type,
                        path,
                        self._root,
                        str(src_path) if src_path else None,
                        str(dest_path) if dest_path else None,
                    )
            except BaseException as err:  # pragma: no cover - defensive callback path
                on_error(err)

    observers: list[Any] = []
    try:
        for p in paths:
            observer = Observer()
            observer.schedule(_Handler(str(os.path.abspath(p))), p, recursive=recursive)
            observer.daemon = True
            observer.start()
            observers.append(observer)
    except Exception:
        for observer in observers:
            with suppress(Exception):
                observer.stop()
        for observer in observers:
            with suppress(Exception):
                observer.join(timeout=1.0)
        raise

    def stop() -> None:
        for observer in observers:
            observer.stop()
        for observer in observers:
            observer.join(timeout=1.0)

    return observers, stop


def from_fs_watch(
    paths: str | list[str],
    *,
    recursive: bool = True,
    debounce: float = 0.1,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    **kwargs: Any,
) -> Node[Any]:
    """Watch filesystem changes and emit debounced events.

    This source intentionally uses event-driven OS watchers only (no polling fallback).
    """
    path_list = [paths] if isinstance(paths, str) else list(paths)
    if len(path_list) == 0:
        msg = "from_fs_watch expects at least one path"
        raise ValueError(msg)
    include_patterns = [_glob_to_regex(p) for p in (include or [])]
    exclude_patterns = [
        _glob_to_regex(p) for p in (exclude or ["**/node_modules/**", "**/.git/**", "**/dist/**"])
    ]

    def normalize_type(event_type: str) -> str:
        low = event_type.lower()
        if low in {"modified", "change", "changed"}:
            return "change"
        if low in {"created", "create"}:
            return "create"
        if low in {"deleted", "delete"}:
            return "delete"
        if low in {"moved", "rename", "renamed"}:
            return "rename"
        return "change"

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        lock = threading.Lock()
        pending: dict[str, dict[str, Any]] = {}
        timer: list[threading.Timer | None] = [None]
        active = [True]
        generation = [0]
        def _noop_stop_backend() -> None:
            return
        stop_backend_ref: list[Callable[[], None]] = [_noop_stop_backend]

        def flush(token: int) -> None:
            batch_msgs: Messages = []
            with lock:
                timer[0] = None
                if not active[0] or not pending:
                    return
                if token != generation[0]:
                    pending.clear()
                    return
                batch_msgs = [(MessageType.DATA, evt.copy()) for evt in pending.values()]
                pending.clear()
            with lock:
                if not active[0] or token != generation[0]:
                    return
            actions.down(batch_msgs)

        def queue_event(
            event_type: str,
            raw_path: str,
            root: str,
            src_path: str | None,
            dest_path: str | None,
        ) -> None:
            normalized_path = os.path.abspath(raw_path).replace("\\", "/")
            normalized_root = os.path.abspath(root).replace("\\", "/")
            rel_path = os.path.relpath(normalized_path, normalized_root).replace("\\", "/")
            included = (
                len(include_patterns) == 0
                or _matches_any(normalized_path, include_patterns)
                or _matches_any(rel_path, include_patterns)
            )
            if not included:
                return
            excluded = _matches_any(normalized_path, exclude_patterns) or _matches_any(
                rel_path, exclude_patterns
            )
            if excluded:
                return
            event = {
                "type": normalize_type(event_type),
                "path": normalized_path,
                "root": normalized_root,
                "relative_path": rel_path,
                "timestamp_ns": wall_clock_ns(),
            }
            if src_path is not None:
                event["src_path"] = os.path.abspath(src_path).replace("\\", "/")
            if dest_path is not None:
                event["dest_path"] = os.path.abspath(dest_path).replace("\\", "/")
            with lock:
                if not active[0]:
                    return
                pending[normalized_path] = event
                if timer[0] is not None:
                    timer[0].cancel()
                token = generation[0]
                t = threading.Timer(debounce, lambda: flush(token))
                t.daemon = True
                t.start()
                timer[0] = t

        def emit_error(err: BaseException) -> None:
            with lock:
                if not active[0]:
                    return
                active[0] = False
                generation[0] += 1
                if timer[0] is not None:
                    timer[0].cancel()
                    timer[0] = None
                pending.clear()
            stop_backend_ref[0]()
            actions.down([(MessageType.ERROR, err)])

        _observers, stop_backend = _build_watchdog_backend(
            path_list,
            recursive,
            queue_event,
            emit_error,
        )
        stop_backend_ref[0] = stop_backend

        def cleanup() -> None:
            with lock:
                active[0] = False
                generation[0] += 1
                if timer[0] is not None:
                    timer[0].cancel()
                    timer[0] = None
                pending.clear()
            stop_backend()

        return cleanup

    return node(start, describe_kind="from_fs_watch", complete_when_deps_complete=False, **kwargs)


def from_webhook(
    register: Callable[
        [
            Callable[[Any], None],
            Callable[[BaseException | Any], None],
            Callable[[], None],
        ],
        Callable[[], None] | None,
    ],
) -> Node[Any]:
    """Bridge HTTP webhook callbacks into a GraphReFly source.

    The ``register`` callback wires your runtime/framework callback into GraphReFly and may return
    cleanup. It receives three functions: ``emit(payload)``, ``error(err)``, and ``complete()``.

    This mirrors the source-adapter style of :func:`from_event_emitter`, but targets HTTP webhook
    handlers from frameworks like FastAPI or Flask.

    Example (FastAPI):
        ```python
        from fastapi import FastAPI, Request
        from graphrefly.extra import from_webhook

        app = FastAPI()
        bridge: dict[str, object] = {}

        def register(emit, error, complete):
            bridge["emit"] = emit
            bridge["error"] = error
            bridge["complete"] = complete
            return None

        webhook_node = from_webhook(register)

        @app.post("/webhook")
        async def webhook(request: Request):
            payload = await request.json()
            bridge["emit"](payload)
            return {"ok": True}
        ```

    Example (Flask):
        ```python
        from flask import Flask, jsonify, request
        from graphrefly.extra import from_webhook

        app = Flask(__name__)
        bridge: dict[str, object] = {}

        def register(emit, error, complete):
            bridge["emit"] = emit
            bridge["error"] = error
            bridge["complete"] = complete
            return None

        webhook_node = from_webhook(register)

        @app.post("/webhook")
        def webhook():
            try:
                bridge["emit"](request.get_json(force=True))
                return jsonify({"ok": True}), 200
            except Exception as exc:
                bridge["error"](exc)
                return jsonify({"ok": False}), 500
        ```
    """

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        active = [True]

        def emit(payload: Any) -> None:
            if not active[0]:
                return
            actions.emit(payload)

        def error(err: BaseException | Any) -> None:
            if not active[0]:
                return
            actions.down([(MessageType.ERROR, err)])

        def complete() -> None:
            if not active[0]:
                return
            actions.down([(MessageType.COMPLETE,)])

        try:
            cleanup = register(emit, error, complete)
        except BaseException as err:
            actions.down([(MessageType.ERROR, err)])
            cleanup = None

        def stop() -> None:
            active[0] = False
            if cleanup is not None:
                cleanup()

        return stop

    return node(start, describe_kind="from_webhook", complete_when_deps_complete=False)


def from_websocket(
    socket: Any | None = None,
    *,
    register: Callable[
        [
            Callable[[Any], None],
            Callable[[BaseException | Any], None],
            Callable[[], None],
        ],
        Callable[[], None] | None,
    ]
    | None = None,
    add_method: str = "add_listener",
    remove_method: str = "remove_listener",
    message_event: str = "message",
    error_event: str = "error",
    close_event: str = "close",
    parse: Callable[[Any], Any] | None = None,
    close_on_cleanup: bool = False,
) -> Node[Any]:
    """Bridge WebSocket events into a GraphReFly source.

    You can either pass a ``register`` callback (preferred in Python for runtime-agnostic wiring)
    or pass a socket-like object with ``add_method``/``remove_method`` listener APIs.

    The ``register`` callback must be atomic: either fully register and return a cleanup callable,
    or raise before any listener side effects.
    """
    if register is None and socket is None:
        msg = "from_websocket requires either socket or register"
        raise ValueError(msg)

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        lock = threading.Lock()
        active = [True]
        cleaned = [False]
        cleanup: Callable[[], None] | None = None

        def _run_cleanup_once() -> None:
            nonlocal cleanup
            fn: Callable[[], None] | None = None
            with lock:
                if cleaned[0]:
                    return
                cleaned[0] = True
                fn = cleanup
            if fn is not None:
                with suppress(Exception):
                    fn()

        def _terminate(msgs: Messages) -> bool:
            with lock:
                if not active[0]:
                    return False
                active[0] = False
            _run_cleanup_once()
            actions.down(msgs)
            return True

        def _extract_payload(value: Any) -> Any:
            if hasattr(value, "data"):
                return value.data
            if isinstance(value, dict) and "data" in value:
                return value["data"]
            return value

        def emit(payload: Any) -> None:
            with lock:
                if not active[0]:
                    return
            try:
                normalized = _extract_payload(payload)
                with lock:
                    if not active[0]:
                        return
                    actions.emit(parse(normalized) if parse is not None else normalized)
            except Exception as err:
                _terminate([(MessageType.ERROR, err)])

        def error(err: BaseException | Any) -> None:
            if isinstance(err, BaseException):
                _terminate([(MessageType.ERROR, err)])
                return
            _terminate([(MessageType.ERROR, RuntimeError(str(err)))])

        def complete() -> None:
            _terminate([(MessageType.COMPLETE,)])

        if register is not None:
            try:
                cleanup = register(emit, error, complete)
                if cleanup is None:
                    raise RuntimeError(
                        "from_websocket register contract violation: "
                        "register must return cleanup callable"
                    )
            except Exception as err:
                _terminate([(MessageType.ERROR, err)])
        else:
            assert socket is not None
            listeners: list[tuple[str, Callable[..., None]]] = []

            def on_message(*args: Any) -> None:
                if len(args) == 1:
                    emit(args[0])
                else:
                    emit(args)

            def on_error(*args: Any) -> None:
                if len(args) == 1:
                    error(args[0])
                else:
                    error(args)

            def on_close(*_args: Any) -> None:
                complete()

            try:
                getattr(socket, add_method)(message_event, on_message)
                listeners.append((message_event, on_message))
                getattr(socket, add_method)(error_event, on_error)
                listeners.append((error_event, on_error))
                getattr(socket, add_method)(close_event, on_close)
                listeners.append((close_event, on_close))
            except Exception as err:
                for event_name, fn in listeners:
                    with suppress(Exception):
                        getattr(socket, remove_method)(event_name, fn)
                _terminate([(MessageType.ERROR, err)])

            def cleanup() -> None:
                for event_name, fn in listeners:
                    with suppress(Exception):
                        getattr(socket, remove_method)(event_name, fn)
                if close_on_cleanup:
                    with suppress(Exception):
                        socket.close()

        def stop() -> None:
            with lock:
                active[0] = False
            _run_cleanup_once()

        return stop

    return node(start, describe_kind="from_websocket", complete_when_deps_complete=False)


def to_websocket(
    source: Node[Any],
    socket: Any | None = None,
    *,
    send: Callable[[Any], None] | None = None,
    close: Callable[..., None] | None = None,
    serialize: Callable[[Any], Any] | None = None,
    close_on_complete: bool = True,
    close_on_error: bool = True,
    close_code: int | None = None,
    close_reason: str | None = None,
    on_transport_error: Callable[[dict[str, Any]], None] | None = None,
) -> Callable[[], None]:
    """Forward upstream DATA payloads to a WebSocket-like transport.

    Transport failures from serialization/send/close are reported through
    ``on_transport_error`` as a dict with ``stage``, ``error``, and ``message`` keys.
    """
    if send is None:
        if socket is None:
            msg = "to_websocket requires socket or send"
            raise ValueError(msg)
        send = socket.send
    if close is None and socket is not None and hasattr(socket, "close"):
        close = socket.close

    def _serialize(value: Any) -> Any:
        if serialize is not None:
            return serialize(value)
        if isinstance(value, (str, bytes, bytearray, memoryview)):
            return value
        try:
            return json.dumps(value)
        except TypeError:
            return str(value)

    closed = [False]

    def _report_transport_error(
        stage: str, err: Exception, message: tuple[Any, ...] | None
    ) -> None:
        if on_transport_error is None:
            return
        with suppress(Exception):
            on_transport_error({"stage": stage, "error": err, "message": message})

    def sink(msgs: Messages) -> None:
        def _close(message: tuple[Any, ...]) -> None:
            if close is None:
                return
            if closed[0]:
                return
            closed[0] = True
            if close_code is None and close_reason is None:
                try:
                    close()
                except Exception as err:
                    _report_transport_error("close", err, message)
                return
            try:
                close(close_code, close_reason)
            except TypeError:
                # Some close callables don't accept code/reason.
                try:
                    close()
                except Exception as err:
                    _report_transport_error("close", err, message)
            except Exception as err:
                _report_transport_error("close", err, message)

        for msg in msgs:
            t = msg[0]
            if t is MessageType.DATA:
                try:
                    payload = _serialize(msg[1] if len(msg) > 1 else None)
                except Exception as err:
                    _report_transport_error("serialize", err, msg)
                    return
                try:
                    send(payload)
                except Exception as err:
                    _report_transport_error("send", err, msg)
                    return
            elif (t is MessageType.COMPLETE and close_on_complete and close is not None) or (
                t is MessageType.ERROR and close_on_error and close is not None
            ):
                _close(msg)

    return source.subscribe(sink)


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


def _sse_frame(event: str, data: str | None = None) -> str:
    out = f"event: {event}\n"
    if data is not None:
        # Preserve trailing empty lines (matches TS split(/\r?\n/) framing behavior).
        normalized = data.replace("\r\n", "\n")
        for line in normalized.split("\n"):
            out += f"data: {line}\n"
    return f"{out}\n"


def to_sse(
    source: Node[Any],
    *,
    serialize: Callable[[Any], str] | None = None,
    data_event: str = "data",
    error_event: str = "error",
    complete_event: str = "complete",
    include_resolved: bool = False,
    include_dirty: bool = False,
    keepalive_s: float | None = None,
    cancel_event: threading.Event | None = None,
    event_name_resolver: Callable[[Any], str] | None = None,
) -> Iterator[str]:
    """Convert node messages into standard SSE text frames.

    This is a sink adapter implemented as a thin subscription bridge over GraphReFly
    messages. The returned iterator yields framed SSE chunks (``event: ...`` and
    ``data: ...`` lines, separated by a blank line).
    """

    import queue

    q: queue.Queue[str | None] = queue.Queue()
    done = threading.Event()

    def encode(value: Any) -> str:
        if isinstance(value, str):
            return value
        if serialize is not None:
            return serialize(value)
        if isinstance(value, BaseException):
            return str(value)
        try:
            return json.dumps(value)
        except TypeError:
            return str(value)

    def sink(msgs: Messages) -> None:
        if done.is_set():
            return
        for msg in msgs:
            t = msg[0]
            if t is MessageType.DATA:
                q.put(_sse_frame(data_event, encode(msg[1] if len(msg) > 1 else None)))
                continue
            if t is MessageType.ERROR:
                q.put(_sse_frame(error_event, encode(msg[1] if len(msg) > 1 else None)))
                done.set()
                q.put(None)
                return
            if t is MessageType.COMPLETE:
                q.put(_sse_frame(complete_event))
                done.set()
                q.put(None)
                return
            if t is MessageType.RESOLVED and not include_resolved:
                continue
            if t is MessageType.DIRTY and not include_dirty:
                continue
            event = event_name_resolver(t) if event_name_resolver is not None else str(t)
            data = encode(msg[1]) if len(msg) > 1 else None
            q.put(_sse_frame(event, data))

    unsub = source.subscribe(sink)

    keepalive_stop = threading.Event()
    keepalive_thread: threading.Thread | None = None
    if keepalive_s is not None and keepalive_s > 0:

        def keepalive_loop() -> None:
            while not keepalive_stop.wait(keepalive_s):
                if done.is_set():
                    return
                q.put(": keepalive\n\n")

        keepalive_thread = threading.Thread(target=keepalive_loop, daemon=True)
        keepalive_thread.start()

    cancel_thread: threading.Thread | None = None
    if cancel_event is not None:

        def cancel_loop() -> None:
            cancel_event.wait()
            if done.is_set():
                return
            done.set()
            q.put(None)

        cancel_thread = threading.Thread(target=cancel_loop, daemon=True)
        cancel_thread.start()

    try:
        while True:
            chunk = q.get()
            if chunk is None:
                break
            yield chunk
    finally:
        done.set()
        keepalive_stop.set()
        if keepalive_thread is not None:
            keepalive_thread.join(timeout=0.05)
        if cancel_thread is not None:
            cancel_thread.join(timeout=0.05)
        unsub()


share_replay = replay


# --- MCP & Git adapters (roadmap §5.2) ----------------------------------------


def from_mcp(
    client: Any,
    *,
    method: str = "notifications/message",
    on_disconnect: Callable[[Callable[[Any], None]], None] | None = None,
    **kwargs: Any,
) -> Node[Any]:
    """Wrap an MCP client's server-push notifications as a reactive source.

    The caller owns the ``Client`` connection (``connect`` / ``close``).  ``from_mcp``
    only registers a notification handler for the chosen *method* and emits each
    notification payload as ``DATA``.

    **Disconnect detection:** MCP SDK does not expose a built-in disconnect event.
    Pass ``on_disconnect`` to wire an external signal (e.g. transport ``close`` event)
    so the source can emit ``ERROR`` and tear down reactively.

    Args:
        client: Any object with a ``set_notification_handler(method, handler)`` method
            (duck-typed — no SDK dependency).
        method: MCP notification method to subscribe to.  Default ``"notifications/message"``.
        on_disconnect: Optional callback ``(cb) -> None`` — call ``cb(err)`` when the
            transport disconnects.

    Returns:
        A :class:`~graphrefly.core.node.Node` emitting one ``DATA`` per server notification.

    Example:
        ```python
        from graphrefly.extra import from_mcp
        tools = from_mcp(client, method="notifications/tools/list_changed")
        ```
    """

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        active = [True]

        def handler(notification: Any) -> None:
            if active[0]:
                actions.emit(notification)

        client.set_notification_handler(method, handler)

        if on_disconnect is not None:

            def _on_dc(err: Any = None) -> None:
                if not active[0]:
                    return
                active[0] = False
                error_value = err if err is not None else Exception("MCP client disconnected")
                actions.down(
                    [(MessageType.ERROR, error_value)]
                )

            on_disconnect(_on_dc)

        def cleanup() -> None:
            active[0] = False
            client.set_notification_handler(method, lambda _n: None)

        return cleanup

    return node(start, describe_kind="producer", **kwargs)


def from_git_hook(
    repo_path: str,
    *,
    poll_ms: int = 5000,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    **kwargs: Any,
) -> Node[Any]:
    """Git change detection as a reactive source.

    Polls for new commits on an interval and emits a structured ``GitEvent`` dict
    whenever HEAD advances.  Zero filesystem side effects — no hook script installation.

    **Limitations:** Polling cannot distinguish commit vs merge vs rebase — ``hook``
    is always ``"post-commit"``.  When multiple commits land between polls, files are
    aggregated but ``message``/``author`` reflect only the latest commit.

    The emitted dict has keys: ``hook``, ``commit``, ``files``, ``message``, ``author``,
    ``timestamp_ns``.

    Cross-repo usage::

        merge([from_git_hook(ts_repo), from_git_hook(py_repo)])

    Args:
        repo_path: Absolute path to the git repository root.
        poll_ms: Polling interval in milliseconds.  Default ``5000``.
        include: Glob patterns — only include matching changed files.
        exclude: Glob patterns — exclude matching changed files.

    Returns:
        A :class:`~graphrefly.core.node.Node` emitting one ``DATA`` per new commit.
    """
    import subprocess

    include_patterns = [_glob_to_regex(p) for p in (include or [])]
    exclude_patterns = [_glob_to_regex(p) for p in (exclude or [])]

    def _git(cmd: list[str]) -> str:
        result = subprocess.run(  # noqa: S603
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        active = [True]
        timer: list[threading.Timer | None] = [None]

        # P4: Seed with current HEAD; route errors through the protocol.
        try:
            last_seen = [_git(["git", "rev-parse", "HEAD"])]
        except Exception as err:
            actions.down([(MessageType.ERROR, err)])
            return lambda: None

        def check() -> None:
            # P7: Top-level guard — any unexpected exception tears down cleanly.
            try:
                _check_inner()
            except Exception as err:
                if active[0]:
                    actions.down([(MessageType.ERROR, err)])
                    cleanup()

        def _check_inner() -> None:
            if not active[0]:
                return
            try:
                head = _git(["git", "rev-parse", "HEAD"])
            except Exception as err:
                if active[0]:
                    actions.down([(MessageType.ERROR, err)])
                    cleanup()
                return

            if not active[0] or head == last_seen[0]:
                schedule()
                return

            try:
                files_raw = _git(["git", "diff", "--name-only", f"{last_seen[0]}..{head}"])
                files = [f for f in files_raw.split("\n") if f]

                if include_patterns:
                    files = [f for f in files if _matches_any(f, include_patterns)]
                if exclude_patterns:
                    files = [f for f in files if not _matches_any(f, exclude_patterns)]

                # P2: Target captured head SHA, not implicit HEAD.
                message = _git(["git", "log", "-1", "--format=%s", head])
                author = _git(["git", "log", "-1", "--format=%an", head])
            except Exception as err:
                if active[0]:
                    actions.down([(MessageType.ERROR, err)])
                    cleanup()
                return

            if not active[0]:
                return
            # P5: Emit before advancing last_seen.
            actions.emit(
                {
                    "hook": "post-commit",
                    "commit": head,
                    "files": files,
                    "message": message,
                    "author": author,
                    "timestamp_ns": wall_clock_ns(),
                }
            )
            last_seen[0] = head
            schedule()

        def schedule() -> None:
            if not active[0]:
                return
            t = threading.Timer(poll_ms / 1000.0, check)
            t.daemon = True
            timer[0] = t
            t.start()

        def cleanup() -> None:
            active[0] = False
            t = timer[0]
            if t is not None:
                t.cancel()
            timer[0] = None

        schedule()
        return cleanup

    return node(start, describe_kind="producer", **kwargs)


__all__ = [
    "cached",
    "empty",
    "first_value_from",
    "for_each",
    "from_any",
    "from_async_iter",
    "from_awaitable",
    "from_cron",
    "from_event_emitter",
    "from_fs_watch",
    "from_git_hook",
    "from_mcp",
    "from_websocket",
    "from_webhook",
    "from_http",
    "from_iter",
    "from_timer",
    "never",
    "of",
    "replay",
    "share",
    "share_replay",
    "throw_error",
    "to_array",
    "to_websocket",
    "to_sse",
    "to_list",
    "HttpBundle",
]
