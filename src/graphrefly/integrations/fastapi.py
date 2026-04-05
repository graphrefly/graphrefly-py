"""FastAPI integration for GraphReFly (roadmap §5.1).

Composable utilities for wiring GraphReFly reactive graphs into FastAPI
applications.  All bridges are **reactive** (subscribe-based, no polling).

Quick start::

    from fastapi import FastAPI, Depends
    from graphrefly import Graph, state
    from graphrefly.integrations.fastapi import (
        graphrefly_lifespan,
        graphrefly_router,
        get_graph,
    )

    g = Graph("app")
    g.add("counter", state(0))

    app = FastAPI(lifespan=graphrefly_lifespan(g))
    app.include_router(graphrefly_router(g, prefix="/graph"))
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request  # noqa: TC002 — runtime introspection by FastAPI
from starlette.responses import StreamingResponse

from graphrefly.compat.async_utils import to_async_iter
from graphrefly.compat.asyncio_runner import AsyncioRunner
from graphrefly.core.protocol import MessageType
from graphrefly.core.runner import set_default_runner
from graphrefly.extra.backpressure import (
    WatermarkOptions,
    create_watermark_controller,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from graphrefly.core.node import Node
    from graphrefly.core.protocol import Messages
    from graphrefly.extra.backpressure import WatermarkController
    from graphrefly.graph.graph import Graph, GraphObserveSource

# ---------------------------------------------------------------------------
# 1. Lifespan
# ---------------------------------------------------------------------------

_APP_STATE_KEY = "graphrefly"


def graphrefly_lifespan(
    *graphs: Graph | dict[str, Graph],
    destroy_on_shutdown: bool = True,
) -> Callable[..., Any]:
    """Return an async context manager suitable for ``FastAPI(lifespan=...)``.

    Accepts a single :class:`Graph`, multiple graphs (positional — keyed as
    ``"0"``, ``"1"``, …), or a ``dict[str, Graph]`` mapping.

    On startup the :class:`~graphrefly.compat.AsyncioRunner` is configured as
    the default runner so async sources (``from_awaitable``, ``from_async_iter``)
    work out of the box.

    On shutdown, each registered graph is destroyed (``graph.destroy()``)
    unless *destroy_on_shutdown* is ``False``.
    """

    @asynccontextmanager
    async def _lifespan(app: Any) -> AsyncIterator[None]:
        runner = AsyncioRunner.from_running()
        set_default_runner(runner)

        registry = _normalize_graphs(graphs)
        app.state.graphrefly = registry

        yield

        set_default_runner(None)
        if destroy_on_shutdown:
            for g in registry.values():
                with suppress(Exception):
                    g.destroy()

    return _lifespan


def _normalize_graphs(
    graphs: tuple[Any, ...],
) -> dict[str, Any]:
    if not graphs:
        raise TypeError(
            "graphrefly_lifespan() requires at least one Graph or "
            "dict argument, e.g. graphrefly_lifespan(my_graph) or "
            'graphrefly_lifespan({"main": g1}).'
        )
    if len(graphs) == 1 and isinstance(graphs[0], dict):
        return dict(graphs[0])
    if len(graphs) == 1:
        return {"default": graphs[0]}
    # Multiple args: require explicit dict keys.
    result: dict[str, Any] = {}
    positional_count = 0
    for g in graphs:
        if isinstance(g, dict):
            for key in g:
                if key in result:
                    raise TypeError(
                        f"Duplicate graph key {key!r} across multiple dict arguments. "
                        "Merge your dicts before passing to graphrefly_lifespan()."
                    )
            result.update(g)
        else:
            positional_count += 1
    if positional_count > 0:
        noun = "argument" if positional_count == 1 else "arguments"
        raise TypeError(
            f"{positional_count} positional Graph {noun} cannot be auto-named. "
            "Pass a dict mapping explicit names to Graph instances instead, e.g. "
            'graphrefly_lifespan({"main": g1, "analytics": g2}).'
        )
    return result


# ---------------------------------------------------------------------------
# 2. Dependency injection
# ---------------------------------------------------------------------------


def get_graph(name: str = "default") -> Any:
    """FastAPI dependency that resolves a :class:`Graph` from the lifespan registry.

    Usage::

        from fastapi import Depends

        @app.get("/describe")
        async def describe(graph = Depends(get_graph())):
            return graph.describe(detail="standard")

        @app.get("/other")
        async def other(graph = Depends(get_graph("analytics"))):
            return graph.describe(detail="standard")
    """
    return _GetGraphDep(name)


class _GetGraphDep:
    """Callable dependency — FastAPI introspects ``__call__`` for injection."""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __call__(self, request: Request) -> Any:
        registry: dict[str, Any] = getattr(request.app.state, _APP_STATE_KEY, {})
        if self._name not in registry:
            raise KeyError(
                f"Graph {self._name!r} not found in lifespan registry. "
                f"Available: {sorted(registry)}"
            )
        return registry[self._name]


# ---------------------------------------------------------------------------
# 3. SSE response
# ---------------------------------------------------------------------------


def sse_response(
    source: Node[Any],
    *,
    serialize: Callable[[Any], str] | None = None,
    data_event: str = "data",
    error_event: str = "error",
    complete_event: str = "complete",
    include_resolved: bool = False,
    keepalive_s: float | None = None,
) -> Any:
    """Return a Starlette ``StreamingResponse`` streaming SSE from *source*.

    Uses the existing :func:`~graphrefly.extra.sources.to_sse` sync iterator
    internally.  Starlette runs sync iterators in a threadpool, so the event
    loop is never blocked.
    """
    from graphrefly.extra.adapters import to_sse

    return StreamingResponse(
        to_sse(
            source,
            serialize=serialize,
            data_event=data_event,
            error_event=error_event,
            complete_event=complete_event,
            include_resolved=include_resolved,
            keepalive_s=keepalive_s,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# 4. WebSocket handler
# ---------------------------------------------------------------------------


async def ws_handler(
    websocket: Any,
    *,
    source: Node[Any] | None = None,
    sink: Node[Any] | None = None,
    serialize: Callable[[Any], str] | None = None,
    deserialize: Callable[[str], Any] | None = None,
    actor: dict[str, Any] | None = None,
) -> None:
    """Bidirectional bridge between a FastAPI ``WebSocket`` and GraphReFly nodes.

    * **source → client**: subscribes to *source* via :func:`to_async_iter` and
      sends each DATA payload as a JSON text frame.
    * **client → sink**: receives JSON text frames and calls
      ``sink.down([(DATA, payload)])`` (with *actor* when provided).

    Either *source* or *sink* (or both) may be ``None`` for one-way bridges.
    The handler runs until the WebSocket closes or the source completes.
    """
    await websocket.accept()

    _ser = serialize or _default_serialize
    _deser = deserialize or json.loads

    async def _send_loop() -> None:
        if source is None:
            return
        async for value in to_async_iter(source):
            await websocket.send_text(_ser(value))

    async def _recv_loop() -> None:
        if sink is None:
            # Still consume frames so the connection stays alive.
            try:
                while True:
                    await websocket.receive_text()
            except Exception:
                return
            return
        try:
            while True:
                raw = await websocket.receive_text()
                payload = _deser(raw)
                if actor is not None:
                    sink.down(
                        [(MessageType.DATA, payload)],
                        actor=actor,
                    )
                else:
                    sink.down([(MessageType.DATA, payload)])
        except Exception:
            return

    try:
        tasks = []
        if source is not None:
            tasks.append(asyncio.create_task(_send_loop()))
        tasks.append(asyncio.create_task(_recv_loop()))
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t
        # Propagate errors from done tasks.
        for t in done:
            if t.exception() is not None:
                raise t.exception()  # type: ignore[misc]
    finally:
        with suppress(Exception):
            await websocket.close()


# ---------------------------------------------------------------------------
# 5. ObserveGateway — graph.observe() → WebSocket (multi-path subscription)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _SubscriptionEntry:
    """Per-path subscription state within :class:`ObserveGateway`."""

    unsub: Any
    wm: Any | None = None


class ObserveGateway:
    """Manages per-client WebSocket subscriptions to graph nodes via ``observe()``.

    A standalone helper that can be wired into any WebSocket endpoint.  Each
    client can subscribe/unsubscribe to individual node paths.  Actor-scoped
    observation respects node guards.

    When *high_water_mark* is set, each per-client subscription creates a
    :class:`WatermarkController`.  The client sends
    ``{"type": "ack", "path": "...", "count": N}`` to drain.

    Example::

        gw = ObserveGateway(graph, high_water_mark=64)

        @app.websocket("/observe")
        async def observe(ws: WebSocket):
            await ws.accept()
            gw.handle_connection(ws)
            try:
                while True:
                    raw = await ws.receive_text()
                    gw.handle_message(ws, raw)
            except WebSocketDisconnect:
                gw.handle_disconnect(ws)
    """

    def __init__(
        self,
        graph: Graph,
        *,
        extract_actor: Callable[..., Any] | None = None,
        high_water_mark: int | None = None,
        low_water_mark: int | None = None,
    ) -> None:
        self._graph = graph
        self._extract_actor = extract_actor or (lambda _client: None)
        self._high_water_mark = high_water_mark
        self._low_water_mark = low_water_mark
        # client → { path → _SubscriptionEntry }
        self._clients: dict[Any, dict[str, _SubscriptionEntry]] = {}

    def handle_connection(self, client: Any) -> None:
        """Register a new client. Call from ``on_connect``."""
        if client not in self._clients:
            self._clients[client] = {}

    def handle_disconnect(self, client: Any) -> None:
        """Unregister a client and dispose all subscriptions."""
        subs = self._clients.pop(client, None)
        if subs is None:
            return
        for entry in subs.values():
            if entry.wm is not None:
                entry.wm.dispose()
            entry.unsub()

    def handle_message(
        self,
        client: Any,
        raw: Any,
        *,
        send: Callable[..., None] | None = None,
    ) -> None:
        """Handle an incoming client message (subscribe/unsubscribe/ack)."""
        sender = send or self._default_send(client)
        try:
            cmd = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            sender({"type": "err", "message": "invalid command"})
            return

        cmd_type = cmd.get("type")
        try:
            if cmd_type == "subscribe":
                self._subscribe(client, cmd["path"], sender)
            elif cmd_type == "unsubscribe":
                self._unsubscribe(client, cmd["path"], sender)
            elif cmd_type == "ack":
                self._ack(client, cmd["path"], cmd.get("count", 1))
            else:
                sender({"type": "err", "message": f"unknown command type: {cmd_type}"})
        except (KeyError, TypeError, ValueError) as exc:
            sender({"type": "err", "message": f"bad command: {exc}"})

    def subscription_count(self, client: Any) -> int:
        """Number of active subscriptions for a client."""
        subs = self._clients.get(client)
        return len(subs) if subs is not None else 0

    def destroy(self) -> None:
        """Dispose all clients and subscriptions."""
        for client in list(self._clients):
            self.handle_disconnect(client)

    # -------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------

    def _subscribe(self, client: Any, path: str, send: Callable[..., None]) -> None:
        subs = self._clients.get(client)
        if subs is None:
            subs = {}
            self._clients[client] = subs
        if path in subs:
            send({"type": "subscribed", "path": path})
            return

        actor = self._extract_actor(client)
        try:
            handle = (
                self._graph.observe(path, actor=actor)
                if actor is not None
                else self._graph.observe(path)
            )
        except Exception as exc:
            send({"type": "err", "message": str(exc)})
            return

        wm: WatermarkController | None = None
        if self._high_water_mark is not None:
            effective_low = (
                self._low_water_mark
                if self._low_water_mark is not None
                else self._high_water_mark // 2
            )
            wm = create_watermark_controller(
                handle.up,
                WatermarkOptions(
                    high_water_mark=self._high_water_mark,
                    low_water_mark=effective_low,
                ),
            )

        unsub_ref: list[Any] = [None]

        def cleanup() -> None:
            if wm is not None:
                wm.dispose()
            if unsub_ref[0] is not None:
                unsub_ref[0]()
            subs.pop(path, None)

        def sink(msgs: Messages) -> None:
            for msg in msgs:
                t = msg[0]
                if t is MessageType.DATA:
                    if wm is not None:
                        wm.on_enqueue()
                    value = msg[1] if len(msg) > 1 else None
                    _try_send(send, {"type": "data", "path": path, "value": value})
                elif t is MessageType.ERROR:
                    payload = msg[1] if len(msg) > 1 else None
                    err_msg = str(payload) if payload is not None else "unknown error"
                    _try_send(send, {"type": "error", "path": path, "error": err_msg})
                    cleanup()
                    return
                elif t is MessageType.COMPLETE or t is MessageType.TEARDOWN:
                    _try_send(send, {"type": "complete", "path": path})
                    cleanup()
                    return

        unsub_ref[0] = handle.subscribe(sink)
        subs[path] = _SubscriptionEntry(unsub=unsub_ref[0], wm=wm)
        send({"type": "subscribed", "path": path})

    def _unsubscribe(self, client: Any, path: str, send: Callable[..., None]) -> None:
        subs = self._clients.get(client)
        entry = subs.pop(path, None) if subs else None
        if entry is not None:
            if entry.wm is not None:
                entry.wm.dispose()
            entry.unsub()
        send({"type": "unsubscribed", "path": path})

    def _ack(self, client: Any, path: str, count: int) -> None:
        subs = self._clients.get(client)
        if subs is None:
            return
        entry = subs.get(path)
        if entry is None:
            return
        wm = entry.wm
        if wm is None:
            return
        n = min(max(0, int(count)), 1024)
        for _ in range(n):
            wm.on_dequeue()

    @staticmethod
    def _default_send(client: Any) -> Callable[..., None]:
        def send(msg: Any) -> None:
            _try_send(lambda m: client.send_text(json.dumps(m)), msg)

        return send


def _try_send(send: Callable[..., None], msg: Any) -> None:
    with suppress(Exception):
        send(msg)


# ---------------------------------------------------------------------------
# 6. Router factory
# ---------------------------------------------------------------------------


def graphrefly_router(
    graph: Any,
    *,
    prefix: str = "",
    actor_resolver: Any | None = None,
    tags: Sequence[str] | None = None,
) -> Any:
    """Pre-built :class:`~fastapi.APIRouter` exposing a graph's HTTP API.

    Endpoints:

    - ``GET  {prefix}/describe``         — :meth:`Graph.describe`
    - ``GET  {prefix}/nodes/{name}``     — :meth:`Graph.get`
    - ``PUT  {prefix}/nodes/{name}``     — :meth:`Graph.set`
    - ``GET  {prefix}/observe``          — SSE of :meth:`Graph.observe` (graph-wide)
    - ``GET  {prefix}/observe/{name}``   — SSE of :meth:`Graph.observe` (single node)
    - ``GET  {prefix}/snapshot``         — :meth:`Graph.snapshot`

    Parameters
    ----------
    graph:
        A :class:`Graph` instance.
    prefix:
        URL prefix for all endpoints.
    actor_resolver:
        Optional FastAPI dependency that returns an :class:`Actor` dict.
        When provided, it is injected into write/signal/observe endpoints.
    tags:
        FastAPI tags for OpenAPI grouping.
    """
    router = APIRouter(prefix=prefix, tags=list(tags or ["graphrefly"]))

    def _resolve_actor(request: Request) -> Any:
        if actor_resolver is not None:
            return actor_resolver(request)
        return None

    @router.get("/describe")
    async def describe(actor: Any = Depends(_resolve_actor)) -> Any:  # noqa: B008
        if actor is not None:
            result = graph.describe(actor=actor, detail="standard")
        else:
            result = graph.describe(detail="standard")
        return result

    @router.get("/nodes/{name:path}")
    async def get_node(name: str) -> Any:
        try:
            value = graph.get(name)
        except KeyError:
            return JSONResponse({"error": f"node {name!r} not found"}, status_code=404)
        return {"name": name, "value": value}

    @router.put("/nodes/{name:path}")
    async def set_node(
        name: str,
        request: Request,
        actor: Any = Depends(_resolve_actor),  # noqa: B008
    ) -> Any:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        if isinstance(body, dict):
            if "value" not in body:
                return JSONResponse({"error": "body must contain a 'value' key"}, status_code=400)
            value = body["value"]
        else:
            value = body

        try:
            if actor is not None:
                graph.set(name, value, actor=actor)
            else:
                graph.set(name, value)
        except KeyError:
            return JSONResponse({"error": f"node {name!r} not found"}, status_code=404)
        return {"ok": True}

    @router.get("/observe")
    async def observe_all(actor: Any = Depends(_resolve_actor)) -> Any:  # noqa: B008
        obs = graph.observe(actor=actor) if actor is not None else graph.observe()
        return _observe_sse_response(obs, graph_wide=True)

    @router.get("/observe/{name:path}")
    async def observe_node(
        name: str,
        actor: Any = Depends(_resolve_actor),  # noqa: B008
    ) -> Any:
        try:
            obs = graph.observe(name, actor=actor) if actor is not None else graph.observe(name)
        except KeyError:
            return JSONResponse({"error": f"node {name!r} not found"}, status_code=404)
        return _observe_sse_response(obs, graph_wide=False)

    @router.get("/snapshot")
    async def snapshot() -> Any:
        return graph.snapshot()

    return router


# ---------------------------------------------------------------------------
# Observe SSE helpers (graph-wide uses path-prefixed events)
# ---------------------------------------------------------------------------


def _observe_sse_response(
    obs: GraphObserveSource,
    *,
    graph_wide: bool,
    high_water_mark: int | None = None,
    low_water_mark: int | None = None,
) -> Any:
    """Build a StreamingResponse for a :class:`GraphObserveSource`.

    When *high_water_mark* is set, a :class:`WatermarkController` sends
    PAUSE upstream when the queue depth exceeds the threshold and RESUME
    when the consumer drains below *low_water_mark* (default:
    ``high_water_mark // 2``).
    """
    from graphrefly.extra.adapters import sse_frame

    q: queue.Queue[str | tuple[str, bool] | None] = queue.Queue()
    done = threading.Event()

    wm: WatermarkController | None = None
    if high_water_mark is not None:
        effective_low = low_water_mark if low_water_mark is not None else high_water_mark // 2
        wm = create_watermark_controller(
            obs.up,
            WatermarkOptions(high_water_mark=high_water_mark, low_water_mark=effective_low),
        )

    # DATA frames are enqueued as ``(sse_string, True)`` tuples so the
    # consumer can call ``wm.on_dequeue()`` only for items that had a
    # matching ``wm.on_enqueue()``.  Non-DATA frames are plain strings.

    if graph_wide:

        def sink(path: str, msgs: Messages) -> None:
            if done.is_set():
                return
            for msg in msgs:
                t = msg[0]
                if t is MessageType.DATA:
                    payload = msg[1] if len(msg) > 1 else None
                    frame = sse_frame(path, _json_encode(payload))
                    if wm is not None:
                        wm.on_enqueue()
                        q.put((frame, True))
                    else:
                        q.put(frame)
                elif t is MessageType.ERROR:
                    payload = msg[1] if len(msg) > 1 else None
                    q.put(sse_frame(f"error:{path}", _json_encode(payload)))
                elif t is MessageType.COMPLETE:
                    q.put(sse_frame(f"complete:{path}"))
                elif t is MessageType.TEARDOWN:
                    done.set()
                    q.put(None)
                    return
    else:

        def sink(msgs: Messages) -> None:  # type: ignore[misc]
            if done.is_set():
                return
            for msg in msgs:
                t = msg[0]
                if t is MessageType.DATA:
                    payload = msg[1] if len(msg) > 1 else None
                    frame = sse_frame("data", _json_encode(payload))
                    if wm is not None:
                        wm.on_enqueue()
                        q.put((frame, True))
                    else:
                        q.put(frame)
                elif t is MessageType.ERROR:
                    payload = msg[1] if len(msg) > 1 else None
                    q.put(sse_frame("error", _json_encode(payload)))
                    done.set()
                    q.put(None)
                    return
                elif t is MessageType.COMPLETE:
                    q.put(sse_frame("complete"))
                    done.set()
                    q.put(None)
                    return
                elif t is MessageType.TEARDOWN:
                    done.set()
                    q.put(None)
                    return

    unsub = obs.subscribe(sink)

    def _iter() -> Any:
        try:
            while not done.is_set():
                try:
                    chunk = q.get(timeout=30)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if chunk is None:
                    break
                if isinstance(chunk, tuple):
                    yield chunk[0]
                    if wm is not None:
                        wm.on_dequeue()
                else:
                    yield chunk
        finally:
            done.set()
            unsub()
            if wm is not None:
                wm.dispose()

    return StreamingResponse(
        _iter(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_serialize(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except TypeError:
        return str(value)


def _json_encode(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, BaseException):
        return str(value)
    try:
        return json.dumps(value)
    except TypeError:
        return str(value)


__all__ = [
    "ObserveGateway",
    "get_graph",
    "graphrefly_lifespan",
    "graphrefly_router",
    "sse_response",
    "ws_handler",
]
