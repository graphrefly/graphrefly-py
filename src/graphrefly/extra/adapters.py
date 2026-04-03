"""Protocol, system, and ingest adapters (roadmap 5.2, 5.3b).

Each adapter wraps an external protocol or system as a reactive :class:`~graphrefly.core.node.Node`
built on :func:`~graphrefly.core.node.node` -- no second protocol.

**Moved from sources.py:** ``from_http``, ``from_websocket`` / ``to_websocket``,
``from_webhook``, ``to_sse``, ``from_mcp``, ``from_git_hook``, ``from_event_emitter``,
``from_fs_watch``, ``sse_frame``, ``HttpBundle``.

**New (5.3b):** ``from_otel``, ``from_syslog`` / ``parse_syslog``, ``from_statsd`` /
``parse_statsd``, ``from_prometheus`` / ``parse_prometheus_text``, ``from_kafka`` /
``to_kafka``, ``from_redis_stream`` / ``to_redis_stream``, ``from_csv`` / ``from_ndjson``,
``from_clickhouse_watch``.
"""

from __future__ import annotations

import csv
import json
import os
import re
import threading
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

from graphrefly.core.clock import wall_clock_ns
from graphrefly.core.node import Node, NodeActions, node
from graphrefly.core.protocol import Messages, MessageType, batch
from graphrefly.extra.resilience import WithStatusBundle, with_status


def _msg_val(m: tuple[Any, ...]) -> Any:
    assert len(m) >= 2
    return m[1]


@dataclass(frozen=True, slots=True)
class SinkTransportError:
    """Error context for sink transport failures (to_kafka, to_redis_stream)."""

    stage: str
    error: Exception
    value: Any


# ---------------------------------------------------------------------------
#  HttpBundle / from_http  (moved from sources.py)
# ---------------------------------------------------------------------------


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
        from graphrefly.extra.adapters import from_http
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


# ---------------------------------------------------------------------------
#  from_event_emitter  (moved from sources.py)
# ---------------------------------------------------------------------------


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
        active = [True]

        def handler(*args: Any) -> None:
            if not active[0]:
                return
            if len(args) == 1:
                actions.emit(args[0])
            else:
                actions.emit(args)

        getattr(emitter, add_method)(event_name, handler)

        def cleanup() -> None:
            active[0] = False
            with suppress(Exception):
                getattr(emitter, remove_method)(event_name, handler)

        return cleanup

    return node(start, describe_kind="from_event_emitter", complete_when_deps_complete=False)


# ---------------------------------------------------------------------------
#  from_fs_watch helpers  (moved from sources.py)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
#  from_webhook  (moved from sources.py)
# ---------------------------------------------------------------------------


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
            active[0] = False
            actions.down([(MessageType.ERROR, err)])

        def complete() -> None:
            if not active[0]:
                return
            active[0] = False
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


# ---------------------------------------------------------------------------
#  from_websocket / to_websocket  (moved from sources.py)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
#  SSE  (moved from sources.py)
# ---------------------------------------------------------------------------


def sse_frame(event: str, data: str | None = None) -> str:
    out = f"event: {event}\n"
    if data is not None:
        # Preserve trailing empty lines (matches TS split(/\\r?\\n/) framing behavior).
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
                q.put(sse_frame(data_event, encode(msg[1] if len(msg) > 1 else None)))
                continue
            if t is MessageType.ERROR:
                q.put(sse_frame(error_event, encode(msg[1] if len(msg) > 1 else None)))
                done.set()
                q.put(None)
                return
            if t is MessageType.COMPLETE:
                q.put(sse_frame(complete_event))
                done.set()
                q.put(None)
                return
            if t is MessageType.RESOLVED and not include_resolved:
                continue
            if t is MessageType.DIRTY and not include_dirty:
                continue
            event = event_name_resolver(t) if event_name_resolver is not None else str(t)
            data = encode(msg[1]) if len(msg) > 1 else None
            q.put(sse_frame(event, data))

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


# ---------------------------------------------------------------------------
#  MCP  (moved from sources.py)
# ---------------------------------------------------------------------------


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
            (duck-typed -- no SDK dependency).
        method: MCP notification method to subscribe to.  Default ``"notifications/message"``.
        on_disconnect: Optional callback ``(cb) -> None`` -- call ``cb(err)`` when the
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
                actions.down([(MessageType.ERROR, error_value)])

            on_disconnect(_on_dc)

        def cleanup() -> None:
            active[0] = False
            client.set_notification_handler(method, lambda _n: None)

        return cleanup

    return node(start, describe_kind="producer", **kwargs)


# ---------------------------------------------------------------------------
#  from_git_hook  (moved from sources.py)
# ---------------------------------------------------------------------------


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
    whenever HEAD advances.  Zero filesystem side effects -- no hook script installation.

    **Limitations:** Polling cannot distinguish commit vs merge vs rebase -- ``hook``
    is always ``"post-commit"``.  When multiple commits land between polls, files are
    aggregated but ``message``/``author`` reflect only the latest commit.

    The emitted dict has keys: ``hook``, ``commit``, ``files``, ``message``, ``author``,
    ``timestamp_ns``.

    Cross-repo usage::

        merge([from_git_hook(ts_repo), from_git_hook(py_repo)])

    Args:
        repo_path: Absolute path to the git repository root.
        poll_ms: Polling interval in milliseconds.  Default ``5000``.
        include: Glob patterns -- only include matching changed files.
        exclude: Glob patterns -- exclude matching changed files.

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
            # P7: Top-level guard -- any unexpected exception tears down cleanly.
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


# ===========================================================================
#  5.3b -- Ingest adapters (universal source layer)
# ===========================================================================


# ---------------------------------------------------------------------------
#  OpenTelemetry (OTLP/HTTP)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OTelBundle:
    """Bundle returned by :func:`from_otel` -- one node per signal type."""

    traces: Node[Any]
    metrics: Node[Any]
    logs: Node[Any]


def from_otel(
    register: Callable[
        [dict[str, Callable[..., None]]],
        Callable[[], None] | None,
    ],
) -> OTelBundle:
    """OTLP/HTTP receiver -- accepts traces, metrics, and logs as separate reactive nodes.

    The caller owns the HTTP server. ``from_otel`` receives a ``register`` callback that
    wires OTLP POST endpoints to the three signal handlers. Each signal type gets its
    own :class:`~graphrefly.core.node.Node` so downstream can subscribe selectively.

    Args:
        register: Callback receiving a dict with ``on_traces``, ``on_metrics``,
            ``on_logs``, ``on_error`` handler functions. Must return a cleanup callable
            or ``None``.

    Returns:
        :class:`OTelBundle` -- ``{ traces, metrics, logs }`` nodes.

    Example:
        ```python
        from graphrefly.extra.adapters import from_otel

        otel = from_otel(lambda h: (
            # wire your HTTP routes to h["on_traces"], h["on_metrics"], h["on_logs"]
            None
        ))
        ```
    """
    active = [True]
    teardown_count = [0]
    register_cleanup: list[Callable[[], None] | None] = [None]

    def _run_register_cleanup() -> None:
        fn = register_cleanup[0]
        if fn is not None:
            register_cleanup[0] = None
            fn()

    def _make_signal_node() -> Node[Any]:
        def start(_deps: list[Any], _actions: NodeActions) -> Callable[[], None]:
            def cleanup() -> None:
                teardown_count[0] += 1
                if teardown_count[0] >= 3:
                    active[0] = False
                    _run_register_cleanup()

            return cleanup

        return node(start, describe_kind="producer", complete_when_deps_complete=False)

    traces = _make_signal_node()
    metrics = _make_signal_node()
    logs = _make_signal_node()

    def _on_traces(spans: list[Any]) -> None:
        if not active[0]:
            return
        with batch():
            for s in spans:
                traces.down([(MessageType.DATA, s)])

    def _on_metrics(ms: list[Any]) -> None:
        if not active[0]:
            return
        with batch():
            for m in ms:
                metrics.down([(MessageType.DATA, m)])

    def _on_logs(ls: list[Any]) -> None:
        if not active[0]:
            return
        with batch():
            for lg in ls:
                logs.down([(MessageType.DATA, lg)])

    def _on_error(err: BaseException | Any) -> None:
        if not active[0]:
            return
        active[0] = False
        for n in (traces, metrics, logs):
            n.down([(MessageType.ERROR, err)])

    register_cleanup[0] = register(
        {
            "on_traces": _on_traces,
            "on_metrics": _on_metrics,
            "on_logs": _on_logs,
            "on_error": _on_error,
        }
    )

    return OTelBundle(traces=traces, metrics=metrics, logs=logs)


# ---------------------------------------------------------------------------
#  Syslog (RFC 5424)
# ---------------------------------------------------------------------------


def parse_syslog(raw: str) -> dict[str, Any]:
    """Parse a raw RFC 5424 syslog line into a structured dict.

    Format: ``<PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID MSG``

    Returns a dict with keys: ``facility``, ``severity``, ``timestamp``, ``hostname``,
    ``app_name``, ``proc_id``, ``msg_id``, ``message``, ``timestamp_ns``.

    Falls back gracefully for unparseable input.
    """
    match = re.match(r"^<(\d{1,3})>\d?\s*(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*(.*)", raw, re.S)
    if not match:
        now_ns = wall_clock_ns()
        timestamp = datetime.fromtimestamp(now_ns / 1e9, tz=UTC).isoformat()
        return {
            "facility": 1,
            "severity": 6,
            "timestamp": timestamp,
            "hostname": "-",
            "app_name": "-",
            "proc_id": "-",
            "msg_id": "-",
            "message": raw.strip(),
            "timestamp_ns": now_ns,
        }
    pri = int(match.group(1))
    return {
        "facility": pri >> 3,
        "severity": pri & 7,
        "timestamp": match.group(2),
        "hostname": match.group(3),
        "app_name": match.group(4),
        "proc_id": match.group(5),
        "msg_id": match.group(6),
        "message": (match.group(7) or "").strip(),
        "timestamp_ns": wall_clock_ns(),
    }


def from_syslog(
    register: Callable[
        [
            Callable[[Any], None],
            Callable[[BaseException | Any], None],
            Callable[[], None],
        ],
        Callable[[], None] | None,
    ],
) -> Node[Any]:
    """RFC 5424 syslog receiver as a reactive source.

    Reuses the :func:`from_webhook` registration pattern. The caller owns the
    UDP/TCP socket and parses raw lines via :func:`parse_syslog` before calling
    ``emit``.

    Args:
        register: Callback wiring socket to ``emit``/``error``/``complete`` handlers.

    Returns:
        A :class:`~graphrefly.core.node.Node` emitting one ``DATA`` per syslog message.
    """
    return from_webhook(register)


# ---------------------------------------------------------------------------
#  StatsD / DogStatsD
# ---------------------------------------------------------------------------

_STATSD_TYPES: dict[str, str] = {
    "c": "counter",
    "g": "gauge",
    "ms": "timer",
    "h": "histogram",
    "s": "set",
    "d": "distribution",
}


def parse_statsd(line: str) -> dict[str, Any]:
    """Parse a raw StatsD/DogStatsD line into a structured dict.

    Format: ``metric.name:value|type|@sampleRate|#tag1:val1,tag2:val2``

    Returns a dict with keys: ``name``, ``value``, ``type``, ``sample_rate`` (optional),
    ``tags``, ``timestamp_ns``.

    Raises :class:`ValueError` on invalid input.
    """
    parts = line.split("|")
    name_value = parts[0] if parts else ""
    split = name_value.split(":")
    if len(split) < 2 or not split[0]:
        msg = f"Invalid StatsD line: {line}"
        raise ValueError(msg)
    name = split[0].strip()
    value_str = split[1].strip()
    type_code = parts[1].strip() if len(parts) > 1 else "c"
    metric_type = _STATSD_TYPES.get(type_code, "counter")
    # Set types use string identifiers, not numeric values.
    if type_code == "s":
        value: float = 0
    else:
        value = float(value_str)

    sample_rate: float | None = None
    tags: dict[str, str] = {}

    for part in parts[2:]:
        p = part.strip()
        if p.startswith("@"):
            sample_rate = float(p[1:])
        elif p.startswith("#"):
            for tag in p[1:].split(","):
                kv = tag.split(":")
                if kv[0]:
                    tags[kv[0]] = kv[1] if len(kv) > 1 else ""

    result: dict[str, Any] = {
        "name": name,
        "value": value,
        "type": metric_type,
        "tags": tags,
        "timestamp_ns": wall_clock_ns(),
    }
    if sample_rate is not None:
        result["sample_rate"] = sample_rate
    return result


def from_statsd(
    register: Callable[
        [
            Callable[[Any], None],
            Callable[[BaseException | Any], None],
            Callable[[], None],
        ],
        Callable[[], None] | None,
    ],
) -> Node[Any]:
    """StatsD/DogStatsD UDP receiver as a reactive source.

    Reuses the :func:`from_webhook` registration pattern. The caller owns the
    UDP socket and parses raw lines via :func:`parse_statsd` before calling ``emit``.

    Args:
        register: Callback wiring socket to ``emit``/``error``/``complete`` handlers.

    Returns:
        A :class:`~graphrefly.core.node.Node` emitting one ``DATA`` per metric line.
    """
    return from_webhook(register)


# ---------------------------------------------------------------------------
#  Prometheus scrape
# ---------------------------------------------------------------------------


def parse_prometheus_text(text: str) -> list[dict[str, Any]]:
    """Parse Prometheus exposition format text into a list of metric dicts.

    Each dict has keys: ``name``, ``labels``, ``value``, ``timestamp_ms`` (optional),
    ``type`` (optional), ``help`` (optional), ``timestamp_ns``.
    """
    results: list[dict[str, Any]] = []
    types: dict[str, str] = {}
    helps: dict[str, str] = {}

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("# TYPE "):
            rest = line[7:]
            space_idx = rest.index(" ") if " " in rest else -1
            if space_idx > 0:
                types[rest[:space_idx]] = rest[space_idx + 1 :].strip()
            continue
        if line.startswith("# HELP "):
            rest = line[7:]
            space_idx = rest.index(" ") if " " in rest else -1
            if space_idx > 0:
                helps[rest[:space_idx]] = rest[space_idx + 1 :].strip()
            continue
        if line.startswith("#"):
            continue

        # metric_name{label="value"} 123 timestamp?
        brace_idx = line.find("{")
        if brace_idx >= 0:
            name = line[:brace_idx]
            close_brace = line.find("}", brace_idx)
            if close_brace < 0:
                continue
            label_str = line[brace_idx + 1 : close_brace]
            labels = _parse_prometheus_labels(label_str)
            after = line[close_brace + 1 :].strip().split()
            value_str = after[0] if after else ""
            ts_str = after[1] if len(after) > 1 else None
        else:
            parts = line.split()
            name = parts[0] if parts else ""
            value_str = parts[1] if len(parts) > 1 else ""
            ts_str = parts[2] if len(parts) > 2 else None
            labels = {}

        if not name or not value_str:
            continue

        base_name = re.sub(r"(_total|_count|_sum|_bucket|_created|_info)$", "", name)
        entry: dict[str, Any] = {
            "name": name,
            "labels": labels,
            "value": float(value_str),
            "timestamp_ns": wall_clock_ns(),
        }
        if ts_str:
            entry["timestamp_ms"] = float(ts_str)
        t = types.get(base_name) or types.get(name)
        if t:
            entry["type"] = t
        h = helps.get(base_name) or helps.get(name)
        if h:
            entry["help"] = h
        results.append(entry)

    return results


def _parse_prometheus_labels(s: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for m in re.finditer(r'(\w+)="((?:[^"\\]|\\.)*)"', s):
        labels[m.group(1)] = re.sub(r"\\(.)", r"\1", m.group(2))
    return labels


def from_prometheus(
    endpoint: str,
    *,
    interval_ns: int = 15_000_000_000,
    headers: dict[str, str] | None = None,
    timeout_ns: int = 10_000_000_000,
) -> Node[Any]:
    """Scrape a Prometheus ``/metrics`` endpoint on a reactive timer interval.

    Each scrape parses the exposition format and emits one ``DATA`` per metric line.
    Uses a timer thread internally (reactive timer source, not busy-wait polling).

    Args:
        endpoint: URL of the Prometheus metrics endpoint.
        interval_ns: Scrape interval in nanoseconds. Default ``15_000_000_000`` (15s).
        headers: Optional request headers.
        timeout_ns: Request timeout in nanoseconds. Default ``10_000_000_000`` (10s).

    Returns:
        A :class:`~graphrefly.core.node.Node` emitting one ``DATA`` per metric per scrape.
    """
    interval_s = interval_ns / 1e9
    timeout_s = timeout_ns / 1e9

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        active = [True]
        running = [False]
        timer: list[threading.Timer | None] = [None]

        def scrape() -> None:
            if not active[0]:
                return
            if running[0]:
                schedule()
                return
            running[0] = True
            try:
                req = urllib.request.Request(endpoint)
                req.add_header("Accept", "text/plain")
                if headers:
                    for k, v in headers.items():
                        req.add_header(k, v)
                with urllib.request.urlopen(req, timeout=timeout_s) as response:
                    if not active[0]:
                        return
                    text = response.read().decode("utf-8")
                    if not active[0]:
                        return
                    prom_metrics = parse_prometheus_text(text)
                    for m in prom_metrics:
                        if not active[0]:
                            return
                        actions.emit(m)
            except Exception as err:
                active[0] = False
                actions.down([(MessageType.ERROR, err)])
                return
            finally:
                running[0] = False
            schedule()

        def schedule() -> None:
            if not active[0]:
                return
            t = threading.Timer(interval_s, scrape)
            t.daemon = True
            t.start()
            timer[0] = t

        # Initial scrape in background thread.
        t = threading.Thread(target=scrape, daemon=True)
        t.start()

        def cleanup() -> None:
            active[0] = False
            if timer[0] is not None:
                timer[0].cancel()
                timer[0] = None

        return cleanup

    return node(start, describe_kind="producer", complete_when_deps_complete=False)


# ---------------------------------------------------------------------------
#  Kafka
# ---------------------------------------------------------------------------


@runtime_checkable
class KafkaConsumerLike(Protocol):
    """Duck-typed Kafka consumer (compatible with confluent-kafka, aiokafka)."""

    def subscribe(self, topics: list[str]) -> None: ...
    def run(self, callback: Callable[..., None]) -> None: ...


@runtime_checkable
class KafkaProducerLike(Protocol):
    """Duck-typed Kafka producer."""

    def send(self, topic: str, *, key: Any = None, value: Any = None) -> None: ...


def from_kafka(
    consumer: Any,
    topic: str,
    *,
    from_beginning: bool = False,
    deserialize: Callable[[Any], Any] | None = None,
) -> Node[Any]:
    """Kafka consumer as a reactive source.

    Wraps a Kafka-compatible consumer. Each message becomes a ``DATA`` emission
    with structured metadata (topic, partition, key, value, headers, offset, timestamp).

    Args:
        consumer: Kafka consumer instance with ``subscribe`` and ``run`` methods
            (caller owns connect/disconnect lifecycle).
        topic: Topic to consume from.
        from_beginning: Start from beginning of topic. Default ``False``.
        deserialize: Optional deserializer for message values. Default: ``json.loads``
            with fallback to string.

    Returns:
        A :class:`~graphrefly.core.node.Node` emitting one ``DATA`` per Kafka message.
    """
    if deserialize is None:

        def _default_deserialize(buf: Any) -> Any:
            if buf is None:
                return None
            raw = buf if isinstance(buf, (str, bytes)) else str(buf)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return raw

        deserialize = _default_deserialize

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        active = [True]

        def _run() -> None:
            try:
                consumer.subscribe([topic])

                def on_message(
                    *,
                    topic: str = "",
                    partition: int = 0,
                    key: Any = None,
                    value: Any = None,
                    headers: dict[str, str] | None = None,
                    offset: str = "0",
                    timestamp: str = "",
                ) -> None:
                    if not active[0]:
                        return
                    actions.emit(
                        {
                            "topic": topic,
                            "partition": partition,
                            "key": str(key) if key is not None else None,
                            "value": deserialize(value),
                            "headers": headers or {},
                            "offset": offset,
                            "timestamp": timestamp,
                            "timestamp_ns": wall_clock_ns(),
                        }
                    )

                consumer.run(on_message)
            except BaseException as err:
                if active[0]:
                    actions.down([(MessageType.ERROR, err)])

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        def cleanup() -> None:
            active[0] = False

        return cleanup

    return node(start, describe_kind="producer", complete_when_deps_complete=False)


def to_kafka(
    source: Node[Any],
    producer: Any,
    topic: str,
    *,
    serialize: Callable[[Any], Any] | None = None,
    key_extractor: Callable[[Any], str | None] | None = None,
    on_transport_error: Callable[[SinkTransportError], None] | None = None,
) -> Callable[[], None]:
    """Kafka producer sink -- forwards upstream ``DATA`` to a Kafka topic.

    Auto-subscribes and returns an unsubscribe function.

    Args:
        source: Upstream node to forward.
        producer: Kafka producer instance with a ``send`` method.
        topic: Target topic.
        serialize: Optional serializer. Default: ``json.dumps``.
        key_extractor: Optional function to extract a message key from the value.
        on_transport_error: Optional callback for transport errors. Receives a
            :class:`SinkTransportError` with ``stage``, ``error``, and ``value``.

    Returns:
        An unsubscribe ``Callable[[], None]`` to tear down the sink.
    """
    if serialize is None:
        serialize = json.dumps

    def _on_message(msg: Any, _index: int, _actions: NodeActions) -> bool:
        if msg[0] is MessageType.DATA:
            value = msg[1] if len(msg) > 1 else None
            key = key_extractor(value) if key_extractor else None
            try:
                serialized = serialize(value)
            except Exception as err:
                if on_transport_error is not None:
                    on_transport_error(
                        SinkTransportError(stage="serialize", error=err, value=value)
                    )
                return True
            try:
                producer.send(topic, key=key, value=serialized)
            except Exception as err:
                if on_transport_error is not None:
                    on_transport_error(SinkTransportError(stage="send", error=err, value=value))
            return True
        return False

    effect = node(
        [source],
        lambda _deps, _actions: lambda: None,
        describe_kind="effect",
        on_message=_on_message,
    )
    unsub = effect.subscribe(lambda _msgs: None)
    return unsub


# ---------------------------------------------------------------------------
#  Redis Streams
# ---------------------------------------------------------------------------


@runtime_checkable
class RedisClientLike(Protocol):
    """Duck-typed Redis client (compatible with redis-py, ioredis)."""

    def xadd(self, name: str, fields: dict[str, str], **kwargs: Any) -> Any: ...
    def xread(self, streams: dict[str, str], **kwargs: Any) -> Any: ...


def from_redis_stream(
    client: Any,
    key: str,
    *,
    block_ms: int = 5000,
    start_id: str = "$",
    parse: Callable[[dict[str, str]], Any] | None = None,
) -> Node[Any]:
    """Redis Streams consumer as a reactive source.

    Uses XREAD with BLOCK to reactively consume stream entries.

    Args:
        client: Redis client instance with ``xread`` method (caller owns connection).
        key: Redis stream key.
        block_ms: Block timeout in ms for XREAD. Default ``5000``.
        start_id: Start ID. Default ``"$"`` (new entries only).
        parse: Optional parser for raw Redis hash fields. Default: parse ``data``
            field as JSON, or return fields dict.

    Returns:
        A :class:`~graphrefly.core.node.Node` emitting one ``DATA`` per stream entry.
    """
    if parse is None:

        def _default_parse(fields: dict[str, str]) -> Any:
            if "data" in fields:
                try:
                    return json.loads(fields["data"])
                except (json.JSONDecodeError, ValueError):
                    return fields["data"]
            return dict(fields)

        parse = _default_parse

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        active = [True]
        last_id = [start_id]

        def poll() -> None:
            while active[0]:
                try:
                    result = client.xread(
                        {key: last_id[0]},
                        block=block_ms,
                    )
                    if not active[0]:
                        return
                    if result:
                        for _stream_key, entries in result:
                            for entry_id, fields in entries:
                                last_id[0] = entry_id
                                actions.emit(
                                    {
                                        "id": entry_id,
                                        "key": key,
                                        "data": parse(fields),
                                        "timestamp_ns": wall_clock_ns(),
                                    }
                                )
                except BaseException as err:
                    if not active[0]:
                        return
                    actions.down([(MessageType.ERROR, err)])
                    return

        t = threading.Thread(target=poll, daemon=True)
        t.start()

        def cleanup() -> None:
            active[0] = False

        return cleanup

    return node(start, describe_kind="producer", complete_when_deps_complete=False)


def to_redis_stream(
    source: Node[Any],
    client: Any,
    key: str,
    *,
    serialize: Callable[[Any], dict[str, str]] | None = None,
    max_len: int | None = None,
    on_transport_error: Callable[[SinkTransportError], None] | None = None,
) -> Callable[[], None]:
    """Redis Streams producer sink -- forwards upstream ``DATA`` to a Redis stream.

    Auto-subscribes and returns an unsubscribe function.

    Args:
        source: Upstream node to forward.
        client: Redis client instance with an ``xadd`` method.
        key: Redis stream key.
        serialize: Optional serializer returning a dict of string fields.
            Default: ``{"data": json.dumps(value)}``.
        max_len: Optional max stream length (MAXLEN ~).
        on_transport_error: Optional callback for transport errors. Receives a
            :class:`SinkTransportError` with ``stage``, ``error``, and ``value``.

    Returns:
        An unsubscribe ``Callable[[], None]`` to tear down the sink.
    """
    if serialize is None:

        def _default_serialize(v: Any) -> dict[str, str]:
            return {"data": json.dumps(v)}

        serialize = _default_serialize

    def _on_message(msg: Any, _index: int, _actions: NodeActions) -> bool:
        if msg[0] is MessageType.DATA:
            value = msg[1] if len(msg) > 1 else None
            try:
                fields = serialize(value)
            except Exception as err:
                if on_transport_error is not None:
                    on_transport_error(
                        SinkTransportError(stage="serialize", error=err, value=value)
                    )
                return True
            try:
                xadd_kwargs: dict[str, Any] = {}
                if max_len is not None:
                    xadd_kwargs["maxlen"] = max_len
                client.xadd(key, fields, **xadd_kwargs)
            except Exception as err:
                if on_transport_error is not None:
                    on_transport_error(SinkTransportError(stage="send", error=err, value=value))
            return True
        return False

    effect = node(
        [source],
        lambda _deps, _actions: lambda: None,
        describe_kind="effect",
        on_message=_on_message,
    )
    unsub = effect.subscribe(lambda _msgs: None)
    return unsub


# ---------------------------------------------------------------------------
#  CSV ingest
# ---------------------------------------------------------------------------


def from_csv(
    source: Iterable[str],
    *,
    delimiter: str = ",",
    has_header: bool = True,
    columns: list[str] | None = None,
    parse_line: Callable[[str], list[str]] | None = None,
) -> Node[Any]:
    """CSV file/stream ingest for batch replay.

    Accepts an ``Iterable[str]`` of CSV lines (file-like or generator) and emits one
    ``DATA`` per row as a dict. ``COMPLETE`` after all rows are emitted.

    Args:
        source: Iterable of CSV text lines.
        delimiter: Column delimiter. Default ``","``.
        has_header: Whether the first row is a header. Default ``True``.
        columns: Explicit column names (overrides header row).
        parse_line: Optional custom line parser. When provided, each line is passed
            to this function instead of using ``csv.reader``. Must return a list of
            field strings.

    Returns:
        A :class:`~graphrefly.core.node.Node` emitting one ``DATA`` per parsed row.
    """

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        active = [True]

        def drain() -> None:
            try:
                headers: list[str] | None = list(columns) if columns else None
                if parse_line is not None:
                    rows_iter = (parse_line(line) for line in source)
                else:
                    rows_iter = csv.reader(source, delimiter=delimiter)
                for row in rows_iter:
                    if not active[0]:
                        return
                    if not any(cell.strip() for cell in row):
                        continue
                    if headers is None and has_header:
                        headers = row
                        continue
                    if headers is None:
                        headers = [f"col{i}" for i in range(len(row))]
                    record: dict[str, str] = {}
                    for i, h in enumerate(headers):
                        record[h] = row[i] if i < len(row) else ""
                    actions.emit(record)
                if active[0]:
                    actions.down([(MessageType.COMPLETE,)])
            except BaseException as err:
                if active[0]:
                    actions.down([(MessageType.ERROR, err)])

        t = threading.Thread(target=drain, daemon=True)
        t.start()

        def cleanup() -> None:
            active[0] = False

        return cleanup

    return node(start, describe_kind="producer", complete_when_deps_complete=False)


# ---------------------------------------------------------------------------
#  NDJSON ingest
# ---------------------------------------------------------------------------


def from_ndjson(source: Iterable[str]) -> Node[Any]:
    """Newline-delimited JSON stream ingest for batch replay.

    Accepts an ``Iterable[str]`` of lines and emits one ``DATA`` per parsed JSON object.
    ``COMPLETE`` after stream ends. Malformed lines emit ``ERROR``.

    Args:
        source: Iterable of NDJSON text lines.

    Returns:
        A :class:`~graphrefly.core.node.Node` emitting one ``DATA`` per JSON line.
    """

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        active = [True]

        def drain() -> None:
            try:
                for line in source:
                    if not active[0]:
                        return
                    trimmed = line.strip()
                    if not trimmed:
                        continue
                    actions.emit(json.loads(trimmed))
                if active[0]:
                    actions.down([(MessageType.COMPLETE,)])
            except BaseException as err:
                if active[0]:
                    actions.down([(MessageType.ERROR, err)])

        t = threading.Thread(target=drain, daemon=True)
        t.start()

        def cleanup() -> None:
            active[0] = False

        return cleanup

    return node(start, describe_kind="producer", complete_when_deps_complete=False)


# ---------------------------------------------------------------------------
#  ClickHouse live materialized view
# ---------------------------------------------------------------------------


@runtime_checkable
class ClickHouseClientLike(Protocol):
    """Duck-typed ClickHouse client."""

    def query(self, query: str, *, format: str = "JSONEachRow") -> Any: ...


def from_clickhouse_watch(
    client: Any,
    query: str,
    *,
    interval_ns: int = 5_000_000_000,
    format: str = "JSONEachRow",
) -> Node[Any]:
    """ClickHouse live materialized view as a reactive source.

    Polls a ClickHouse query on a reactive timer interval and emits rows.
    Uses a timer-driven approach (not busy-wait polling).

    Args:
        client: ClickHouse client instance with a ``query`` method (caller owns connection).
        query: SQL query to execute on each interval.
        interval_ns: Polling interval in nanoseconds. Default ``5_000_000_000`` (5s).
        format: JSON format to request. Default ``"JSONEachRow"``.

    Returns:
        A :class:`~graphrefly.core.node.Node` emitting one ``DATA`` per result row per scrape.
    """
    interval_s = interval_ns / 1e9

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        active = [True]
        running = [False]
        timer: list[threading.Timer | None] = [None]

        def execute() -> None:
            if not active[0]:
                return
            if running[0]:
                schedule()
                return
            running[0] = True
            try:
                result = client.query(query, format=format)
                if not active[0]:
                    return
                rows = result if isinstance(result, list) else list(result)
                for row in rows:
                    if not active[0]:
                        return
                    actions.emit(row)
            except Exception as err:
                active[0] = False
                actions.down([(MessageType.ERROR, err)])
                return
            finally:
                running[0] = False
            schedule()

        def schedule() -> None:
            if not active[0]:
                return
            t = threading.Timer(interval_s, execute)
            t.daemon = True
            t.start()
            timer[0] = t

        # Initial execute in background thread.
        t = threading.Thread(target=execute, daemon=True)
        t.start()

        def cleanup() -> None:
            active[0] = False
            if timer[0] is not None:
                timer[0].cancel()
                timer[0] = None

        return cleanup

    return node(start, describe_kind="producer", complete_when_deps_complete=False)


# ---------------------------------------------------------------------------
#  __all__
# ---------------------------------------------------------------------------

__all__ = [
    # Moved from sources.py
    "HttpBundle",
    "from_http",
    "from_event_emitter",
    "from_fs_watch",
    "from_webhook",
    "from_websocket",
    "to_websocket",
    "sse_frame",
    "to_sse",
    "from_mcp",
    "from_git_hook",
    # 5.3b -- Ingest adapters
    "SinkTransportError",
    "OTelBundle",
    "from_otel",
    "parse_syslog",
    "from_syslog",
    "parse_statsd",
    "from_statsd",
    "parse_prometheus_text",
    "from_prometheus",
    "from_kafka",
    "to_kafka",
    "from_redis_stream",
    "to_redis_stream",
    "from_csv",
    "from_ndjson",
    "from_clickhouse_watch",
    "ClickHouseClientLike",
]
