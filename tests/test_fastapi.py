"""Tests for FastAPI integration (roadmap §5.1)."""

from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import Depends, FastAPI, WebSocket
from starlette.testclient import TestClient

from graphrefly import Graph, derived, state
from graphrefly.core.protocol import MessageType
from graphrefly.integrations.fastapi import (
    get_graph,
    graphrefly_lifespan,
    graphrefly_router,
    sse_response,
    ws_handler,
)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


def test_lifespan_registers_graphs() -> None:
    """Lifespan stores graphs in app.state.graphrefly."""
    g = Graph("test")
    g.add("x", state(1))
    app = FastAPI(lifespan=graphrefly_lifespan(g))

    with TestClient(app):
        assert hasattr(app.state, "graphrefly")
        assert "default" in app.state.graphrefly
        assert app.state.graphrefly["default"] is g


def test_lifespan_destroys_graphs() -> None:
    """Graphs are destroyed on shutdown when destroy_on_shutdown=True (default)."""
    g = Graph("test")
    g.add("x", state(1))
    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=True))

    with TestClient(app):
        assert len(g._nodes) == 1

    # After shutdown, destroy() clears the node registry.
    assert len(g._nodes) == 0


def test_lifespan_no_destroy() -> None:
    """destroy_on_shutdown=False leaves graphs intact."""
    g = Graph("test")
    g.add("x", state(1))
    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))

    with TestClient(app):
        pass

    assert len(g._nodes) == 1


def test_lifespan_dict_registry() -> None:
    """Multiple graphs registered by dict key."""
    g1 = Graph("one")
    g1.add("a", state(1))
    g2 = Graph("two")
    g2.add("b", state(2))

    app = FastAPI(lifespan=graphrefly_lifespan({"main": g1, "aux": g2}, destroy_on_shutdown=False))
    app.include_router(graphrefly_router(g1, prefix="/main"))
    app.include_router(graphrefly_router(g2, prefix="/aux"))

    with TestClient(app) as client:
        r1 = client.get("/main/nodes/a")
        assert r1.status_code == 200
        assert r1.json()["value"] == 1

        r2 = client.get("/aux/nodes/b")
        assert r2.status_code == 200
        assert r2.json()["value"] == 2


# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------


def test_get_graph_dependency() -> None:
    """get_graph() resolves from app.state.graphrefly."""
    g = Graph("test")
    g.add("x", state(42))
    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))

    @app.get("/value")
    async def read_value(graph: Any = Depends(get_graph())) -> Any:  # noqa: B008
        return {"value": graph.get("x")}

    with TestClient(app) as client:
        resp = client.get("/value")
        assert resp.status_code == 200
        assert resp.json()["value"] == 42


def test_get_graph_missing_key() -> None:
    """get_graph() raises KeyError for unregistered graph name."""
    g = Graph("test")
    g.add("x", state(1))
    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))

    @app.get("/bad")
    async def bad(graph: Any = Depends(get_graph("nonexistent"))) -> Any:  # noqa: B008
        return {}

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/bad")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Router — GET /nodes/{name}
# ---------------------------------------------------------------------------


def test_router_get_node() -> None:
    g = Graph("test")
    g.add("counter", state(99))
    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))
    app.include_router(graphrefly_router(g))

    with TestClient(app) as client:
        resp = client.get("/nodes/counter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "counter"
        assert data["value"] == 99


def test_router_get_node_not_found() -> None:
    g = Graph("test")
    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))
    app.include_router(graphrefly_router(g))

    with TestClient(app) as client:
        resp = client.get("/nodes/missing")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Router — PUT /nodes/{name}
# ---------------------------------------------------------------------------


def test_router_set_node() -> None:
    g = Graph("test")
    g.add("counter", state(0))
    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))
    app.include_router(graphrefly_router(g))

    with TestClient(app) as client:
        resp = client.put("/nodes/counter", json={"value": 42})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert g.get("counter") == 42


def test_router_set_node_not_found() -> None:
    g = Graph("test")
    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))
    app.include_router(graphrefly_router(g))

    with TestClient(app) as client:
        resp = client.put("/nodes/missing", json={"value": 1})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Router — GET /describe
# ---------------------------------------------------------------------------


def test_router_describe() -> None:
    g = Graph("test")
    g.add("a", state(1))
    g.add("b", derived([g.node("a")], lambda deps, _: deps[0] * 2))
    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))
    app.include_router(graphrefly_router(g))

    with TestClient(app) as client:
        resp = client.get("/describe")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data


# ---------------------------------------------------------------------------
# Router — GET /snapshot
# ---------------------------------------------------------------------------


def test_router_snapshot() -> None:
    g = Graph("test")
    g.add("x", state(7))
    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))
    app.include_router(graphrefly_router(g))

    with TestClient(app) as client:
        resp = client.get("/snapshot")
        assert resp.status_code == 200
        snap = resp.json()
        assert snap["name"] == "test"


# ---------------------------------------------------------------------------
# Router — GET /observe/{name} (SSE)
# ---------------------------------------------------------------------------


def test_router_observe_node_sse() -> None:
    """SSE stream from a single node emits data events."""
    g = Graph("test")
    s = state(0)
    g.add("x", s)
    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))
    app.include_router(graphrefly_router(g))

    with TestClient(app) as client:
        # Set the value after a short delay in a background thread.
        def _set_then_complete() -> None:
            time.sleep(0.1)
            g.set("x", 42)
            time.sleep(0.05)
            s.down([(MessageType.COMPLETE,)])

        t = threading.Thread(target=_set_then_complete, daemon=True)
        t.start()

        with client.stream("GET", "/observe/x") as resp:
            assert resp.headers["content-type"] == "text/event-stream; charset=utf-8"
            text = ""
            for chunk in resp.iter_text():
                text += chunk
                if "event: complete" in text:
                    break

        t.join(timeout=2)
        assert "event: data" in text
        assert "42" in text


# ---------------------------------------------------------------------------
# SSE response helper
# ---------------------------------------------------------------------------


def test_sse_response_from_node() -> None:
    """sse_response() wraps a node in a StreamingResponse."""
    from starlette.responses import StreamingResponse

    s = state(0)
    resp = sse_response(s)
    assert isinstance(resp, StreamingResponse)
    assert resp.media_type == "text/event-stream"
    s.unsubscribe()


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------


def test_ws_handler_source_to_client() -> None:
    """ws_handler sends source DATA to the WebSocket client."""
    g = Graph("test")
    s = state(0)
    g.add("out", s)

    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await ws_handler(websocket, source=g.node("out"))

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        g.set("out", {"msg": "hello"})
        data = ws.receive_json()
        assert data == {"msg": "hello"}


def test_ws_handler_client_to_sink() -> None:
    """ws_handler forwards client messages to sink node."""
    g = Graph("test")
    s = state(0)
    g.add("in", s)

    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await ws_handler(websocket, sink=g.node("in"))

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json(99)
        time.sleep(0.2)
        assert g.get("in") == 99


# ---------------------------------------------------------------------------
# Router with actor_resolver
# ---------------------------------------------------------------------------


def test_router_actor_resolver() -> None:
    """actor_resolver dependency is injected into describe endpoint."""
    g = Graph("test")
    g.add("x", state(0))

    resolved_actors: list[Any] = []

    def my_actor_resolver(request: Any) -> dict[str, Any]:
        actor = {"type": "human", "id": "user-1"}
        resolved_actors.append(actor)
        return actor

    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))
    app.include_router(graphrefly_router(g, actor_resolver=my_actor_resolver))

    with TestClient(app) as client:
        resp = client.get("/describe")
        assert resp.status_code == 200
        assert len(resolved_actors) == 1
        assert resolved_actors[0]["id"] == "user-1"


# ---------------------------------------------------------------------------
# Router with prefix
# ---------------------------------------------------------------------------


def test_router_prefix() -> None:
    """Router prefix is applied to all endpoints."""
    g = Graph("test")
    g.add("x", state(5))
    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))
    app.include_router(graphrefly_router(g, prefix="/api/v1"))

    with TestClient(app) as client:
        resp = client.get("/api/v1/nodes/x")
        assert resp.status_code == 200
        assert resp.json()["value"] == 5

        resp2 = client.get("/nodes/x")
        assert resp2.status_code == 404


# ---------------------------------------------------------------------------
# Reactive round-trip: set via PUT, read derived via GET
# ---------------------------------------------------------------------------


def test_reactive_round_trip() -> None:
    """Setting a state node via PUT propagates to derived nodes read via GET."""
    g = Graph("test")
    g.add("a", state(1))
    b = derived([g.node("a")], lambda deps, _: deps[0] * 10)
    g.add("b", b)

    # Subscribe so the derived node is live and computes eagerly.
    unsub = b.subscribe(lambda _msgs: None)

    app = FastAPI(lifespan=graphrefly_lifespan(g, destroy_on_shutdown=False))
    app.include_router(graphrefly_router(g))

    with TestClient(app) as client:
        resp = client.get("/nodes/b")
        assert resp.json()["value"] == 10

        client.put("/nodes/a", json={"value": 5})

        # Derived updates synchronously through the reactive graph.
        resp2 = client.get("/nodes/b")
        assert resp2.json()["value"] == 50

    unsub()
