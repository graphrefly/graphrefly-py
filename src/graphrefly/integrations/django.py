"""Django integration for GraphReFly (roadmap §5.1).

Composable utilities for wiring GraphReFly reactive graphs into Django
applications.  All bridges are **reactive** (subscribe-based, no polling).

Quick start::

    # settings.py
    INSTALLED_APPS = [
        "graphrefly.integrations.django",
        ...
    ]
    GRAPHREFLY_GRAPHS = {
        "default": lambda: build_my_graph(),
    }

    # urls.py
    from graphrefly.integrations.django import graphrefly_urlpatterns

    urlpatterns = [
        *graphrefly_urlpatterns(prefix="graph/"),
    ]

Alternatively, manage graphs manually without the AppConfig::

    from graphrefly import Graph, state
    from graphrefly.integrations.django import (
        get_graph,
        register_graph,
        graphrefly_urlpatterns,
    )

    g = Graph("app")
    g.add("counter", state(0))
    register_graph(g)

    urlpatterns = [
        *graphrefly_urlpatterns(g, prefix="graph/"),
    ]
"""

from __future__ import annotations

import json
import threading
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from graphrefly.core.protocol import MessageType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from graphrefly.core.node import Node
    from graphrefly.core.protocol import Messages
    from graphrefly.graph.graph import Graph, GraphObserveSource

# ---------------------------------------------------------------------------
# 1. Graph registry
# ---------------------------------------------------------------------------

_registry: dict[str, Graph] = {}
_registry_lock = threading.Lock()
_atexit_registered = False


def register_graph(graph: Graph, name: str = "default") -> None:
    """Register a :class:`Graph` in the module-level registry.

    Args:
        graph: The graph instance to register.
        name: Registry key (default: ``"default"``).
    """
    with _registry_lock:
        _registry[name] = graph


def unregister_graph(name: str = "default") -> Graph | None:
    """Remove and return a graph from the registry.

    Returns ``None`` if *name* is not registered.
    """
    with _registry_lock:
        return _registry.pop(name, None)


def get_graph(name: str = "default") -> Graph:
    """Retrieve a registered :class:`Graph` by name.

    Raises:
        KeyError: If *name* is not in the registry.
    """
    with _registry_lock:
        try:
            return _registry[name]
        except KeyError:
            raise KeyError(
                f"Graph {name!r} not found in registry. Available: {sorted(_registry)}"
            ) from None


def _clear_registry() -> None:
    """Remove all graphs from the registry (for testing)."""
    with _registry_lock:
        _registry.clear()


# ---------------------------------------------------------------------------
# 2. Django AppConfig
# ---------------------------------------------------------------------------


try:
    from django.apps import AppConfig as _DjangoAppConfig  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    _DjangoAppConfig = None


if _DjangoAppConfig is not None:

    class GraphReflyConfig(_DjangoAppConfig):  # type: ignore[misc]
        """Django ``AppConfig`` that builds and registers graphs on startup.

        Add ``"graphrefly.integrations.django"`` to ``INSTALLED_APPS`` and
        define ``GRAPHREFLY_GRAPHS`` in your settings::

            GRAPHREFLY_GRAPHS = {
                "default": lambda: my_graph_factory(),
            }

        Each value is a callable returning a :class:`Graph`.  On ``ready()``,
        the factories are called and registered via :func:`register_graph`.

        On server shutdown, registered graphs are destroyed unless
        ``GRAPHREFLY_DESTROY_ON_SHUTDOWN = False``.
        """

        name = "graphrefly.integrations.django"
        label = "graphrefly"
        verbose_name = "GraphReFly"

        def ready(self) -> None:
            from django.conf import settings  # type: ignore[import-untyped]

            graph_factories: dict[str, Callable[[], Any]] = getattr(
                settings, "GRAPHREFLY_GRAPHS", {}
            )
            for key, factory in graph_factories.items():
                register_graph(factory(), name=key)

            # Register shutdown handler to destroy graphs.
            global _atexit_registered  # noqa: PLW0603
            destroy = getattr(settings, "GRAPHREFLY_DESTROY_ON_SHUTDOWN", True)
            if destroy and not _atexit_registered:
                import atexit

                atexit.register(_destroy_all_graphs)
                _atexit_registered = True

    # Django discovers AppConfig via the module's `default_app_config` or
    # the `default_auto_field` convention.  For modern Django (3.2+), the
    # preferred approach is the AppConfig class itself.
    default_app_config = "graphrefly.integrations.django.GraphReflyConfig"


def _destroy_all_graphs() -> None:
    """Destroy all registered graphs (called at shutdown)."""
    with _registry_lock:
        graphs = list(_registry.values())
        _registry.clear()
    for g in graphs:
        with suppress(Exception):
            g.destroy()


# ---------------------------------------------------------------------------
# 3. View helpers
# ---------------------------------------------------------------------------


def _json_response(data: Any, status: int = 200) -> Any:
    """Return a Django ``JsonResponse``."""
    from django.http import JsonResponse  # type: ignore[import-untyped]

    return JsonResponse(data, status=status, safe=False)


def _json_error(message: str, status: int) -> Any:
    from django.http import JsonResponse

    return JsonResponse({"error": message}, status=status)


def _parse_json_body(request: Any) -> tuple[Any, str | None]:
    """Parse JSON from a Django request body.

    Returns ``(parsed, None)`` on success or ``(None, error_message)`` on failure.
    """
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return None, "invalid JSON body"
    return body, None


# ---------------------------------------------------------------------------
# 4. SSE response
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
    """Return a Django ``StreamingHttpResponse`` streaming SSE from *source*.

    Uses :func:`~graphrefly.extra.adapters.to_sse` internally.
    Django runs sync iterators in a threadpool for ASGI deployments.
    """
    from django.http import StreamingHttpResponse

    from graphrefly.extra.adapters import to_sse

    response = StreamingHttpResponse(
        to_sse(
            source,
            serialize=serialize,
            data_event=data_event,
            error_event=error_event,
            complete_event=complete_event,
            include_resolved=include_resolved,
            keepalive_s=keepalive_s,
        ),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


# ---------------------------------------------------------------------------
# 5. Observe SSE (graph.observe → SSE stream)
# ---------------------------------------------------------------------------


def _observe_sse_response(
    obs: GraphObserveSource,
    *,
    graph_wide: bool,
    include_resolved: bool = False,
    high_water_mark: int | None = None,
    low_water_mark: int | None = None,
) -> Any:
    """Build a ``StreamingHttpResponse`` for a :class:`GraphObserveSource`.

    When *high_water_mark* is set, a :class:`WatermarkController` sends
    PAUSE upstream when the queue depth exceeds the threshold and RESUME
    when the consumer drains below *low_water_mark* (default:
    ``high_water_mark // 2``).

    When *include_resolved* is ``True``, RESOLVED messages are forwarded
    as ``event: resolved`` (or ``resolved:<path>`` for graph-wide) SSE
    frames.
    """
    import queue

    from django.http import StreamingHttpResponse

    from graphrefly.extra.adapters import sse_frame
    from graphrefly.extra.backpressure import (
        WatermarkOptions,
        create_watermark_controller,
    )

    q: queue.Queue[str | tuple[str, bool] | None] = queue.Queue()
    done = threading.Event()

    wm: Any = None
    if high_water_mark is not None:
        effective_low = low_water_mark if low_water_mark is not None else high_water_mark // 2
        wm = create_watermark_controller(
            obs.up,
            WatermarkOptions(high_water_mark=high_water_mark, low_water_mark=effective_low),
        )

    def _enqueue_data(frame: str) -> None:
        if wm is not None:
            wm.on_enqueue()
            q.put((frame, True))
        else:
            q.put(frame)

    if graph_wide:

        def sink(path: str, msgs: Messages) -> None:
            if done.is_set():
                return
            for msg in msgs:
                t = msg[0]
                if t is MessageType.DATA:
                    payload = msg[1] if len(msg) > 1 else None
                    _enqueue_data(sse_frame(path, _json_encode(payload)))
                elif t is MessageType.RESOLVED and include_resolved:
                    q.put(sse_frame(f"resolved:{path}"))
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
                    _enqueue_data(sse_frame("data", _json_encode(payload)))
                elif t is MessageType.RESOLVED and include_resolved:
                    q.put(sse_frame("resolved"))
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

    def _iter() -> Iterator[str]:
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

    response = StreamingHttpResponse(_iter(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


# ---------------------------------------------------------------------------
# 6. URL pattern factory
# ---------------------------------------------------------------------------


def graphrefly_urlpatterns(
    graph: Graph | None = None,
    *,
    prefix: str = "",
    graph_name: str = "default",
    actor_resolver: Callable[..., Any] | None = None,
    include_resolved: bool = False,
    high_water_mark: int | None = None,
    low_water_mark: int | None = None,
) -> list[Any]:
    """Return Django URL patterns exposing a graph's HTTP API.

    Endpoints (all prefixed by *prefix*):

    - ``GET  describe/``           — :meth:`Graph.describe`
    - ``GET  nodes/<path:name>/``  — :meth:`Graph.get`
    - ``PUT  nodes/<path:name>/``  — :meth:`Graph.set`
    - ``GET  observe/``            — SSE of :meth:`Graph.observe` (graph-wide)
    - ``GET  observe/<path:name>/``— SSE of :meth:`Graph.observe` (single node)
    - ``GET  snapshot/``           — :meth:`Graph.snapshot`

    Parameters
    ----------
    graph:
        A :class:`Graph` instance.  If ``None``, the graph is resolved from
        the registry via *graph_name* on each request.
    prefix:
        URL prefix for all endpoints (e.g. ``"graph/"``).
    graph_name:
        Registry key used when *graph* is ``None``.
    actor_resolver:
        Optional callable ``(request) -> dict`` for guard-scoped endpoints.
    include_resolved:
        When ``True``, RESOLVED messages are forwarded as SSE events.
    high_water_mark:
        When set, observe SSE endpoints apply watermark backpressure.
    low_water_mark:
        Low watermark threshold (default: ``high_water_mark // 2``).
    """
    from django.urls import path  # type: ignore[import-untyped]

    def _get_graph(request: Any) -> Any:
        if graph is not None:
            return graph
        return get_graph(graph_name)

    def _resolve_actor(request: Any) -> Any:
        if actor_resolver is not None:
            return actor_resolver(request)
        return None

    def _require_get(request: Any) -> Any | None:
        if request.method != "GET":
            return _json_error("method not allowed", 405)
        return None

    def describe_view(request: Any) -> Any:
        if (err := _require_get(request)) is not None:
            return err
        g = _get_graph(request)
        actor = _resolve_actor(request)
        if actor is not None:
            result = g.describe(actor=actor, detail="standard")
        else:
            result = g.describe(detail="standard")
        return _json_response(result)

    def node_view(request: Any, name: str) -> Any:
        if request.method not in ("GET", "PUT"):
            return _json_error("method not allowed", 405)
        g = _get_graph(request)
        if request.method == "GET":
            try:
                value = g.get(name)
            except KeyError:
                return _json_error(f"node {name!r} not found", 404)
            return _json_response({"name": name, "value": value})

        # PUT
        actor = _resolve_actor(request)
        body, err = _parse_json_body(request)
        if err is not None:
            return _json_error(err, 400)

        if isinstance(body, dict):
            if "value" not in body:
                return _json_error("body must contain a 'value' key", 400)
            value = body["value"]
        else:
            value = body

        try:
            if actor is not None:
                g.set(name, value, actor=actor)
            else:
                g.set(name, value)
        except KeyError:
            return _json_error(f"node {name!r} not found", 404)
        return _json_response({"ok": True})

    def observe_all_view(request: Any) -> Any:
        if (err := _require_get(request)) is not None:
            return err
        g = _get_graph(request)
        actor = _resolve_actor(request)
        obs = g.observe(actor=actor) if actor is not None else g.observe()
        return _observe_sse_response(
            obs,
            graph_wide=True,
            include_resolved=include_resolved,
            high_water_mark=high_water_mark,
            low_water_mark=low_water_mark,
        )

    def observe_node_view(request: Any, name: str) -> Any:
        if (err := _require_get(request)) is not None:
            return err
        g = _get_graph(request)
        actor = _resolve_actor(request)
        try:
            obs = g.observe(name, actor=actor) if actor is not None else g.observe(name)
        except KeyError:
            return _json_error(f"node {name!r} not found", 404)
        return _observe_sse_response(
            obs,
            graph_wide=False,
            include_resolved=include_resolved,
            high_water_mark=high_water_mark,
            low_water_mark=low_water_mark,
        )

    def snapshot_view(request: Any) -> Any:
        if (err := _require_get(request)) is not None:
            return err
        g = _get_graph(request)
        return _json_response(g.snapshot())

    return [
        path(f"{prefix}describe/", describe_view, name="graphrefly-describe"),
        path(f"{prefix}nodes/<path:name>/", node_view, name="graphrefly-node"),
        path(f"{prefix}observe/", observe_all_view, name="graphrefly-observe-all"),
        path(f"{prefix}observe/<path:name>/", observe_node_view, name="graphrefly-observe-node"),
        path(f"{prefix}snapshot/", snapshot_view, name="graphrefly-snapshot"),
    ]


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class GraphReflyMiddleware:
    """Django middleware that attaches the graph registry to ``request.graphrefly``.

    Add to ``MIDDLEWARE``::

        MIDDLEWARE = [
            "graphrefly.integrations.django.GraphReflyMiddleware",
            ...
        ]

    Then in any view::

        def my_view(request):
            graph = request.graphrefly["default"]
            value = graph.get("counter")
    """

    def __init__(self, get_response: Callable[..., Any]) -> None:
        self.get_response = get_response

    def __call__(self, request: Any) -> Any:
        with _registry_lock:
            request.graphrefly = dict(_registry)
        return self.get_response(request)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    "GraphReflyMiddleware",
    "get_graph",
    "graphrefly_urlpatterns",
    "register_graph",
    "sse_response",
    "unregister_graph",
]
