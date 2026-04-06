"""Tests for Django integration (roadmap §5.1)."""

from __future__ import annotations

import json
import threading
from typing import Any

import django
from django.conf import settings

# Configure Django settings before any Django imports.
if not settings.configured:
    settings.configure(
        DEBUG=True,
        DATABASES={},
        INSTALLED_APPS=[],
        ROOT_URLCONF=__name__,
        SECRET_KEY="test-secret-key",
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    )
    django.setup()

from django.test import RequestFactory

from graphrefly import Graph, derived, state
from graphrefly.core.protocol import MessageType
from graphrefly.integrations.django import (
    GraphReflyMiddleware,
    _clear_registry,
    get_graph,
    graphrefly_urlpatterns,
    register_graph,
    sse_response,
    unregister_graph,
)

# RequestFactory for creating test requests.
rf = RequestFactory()

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _setup() -> None:
    """Clear registry before each test."""
    _clear_registry()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_register_and_get_graph() -> None:
    _setup()
    g = Graph("test")
    g.add("x", state(1))
    register_graph(g)
    assert get_graph() is g


def test_register_named_graph() -> None:
    _setup()
    g1 = Graph("one")
    g2 = Graph("two")
    register_graph(g1, name="a")
    register_graph(g2, name="b")
    assert get_graph("a") is g1
    assert get_graph("b") is g2


def test_get_graph_missing_key() -> None:
    _setup()
    import pytest

    with pytest.raises(KeyError, match="not found"):
        get_graph("nonexistent")


def test_unregister_graph() -> None:
    _setup()
    g = Graph("test")
    register_graph(g)
    removed = unregister_graph()
    assert removed is g

    import pytest

    with pytest.raises(KeyError):
        get_graph()


def test_unregister_missing() -> None:
    _setup()
    assert unregister_graph("nope") is None


# ---------------------------------------------------------------------------
# URL patterns — GET /nodes/<name>
# ---------------------------------------------------------------------------


def test_urlpatterns_get_node() -> None:
    _setup()
    g = Graph("test")
    g.add("counter", state(99))

    patterns = graphrefly_urlpatterns(g)
    # Find the get-node view.
    view = _find_view(patterns, "graphrefly-node")

    request = rf.get("/nodes/counter")
    resp = view(request, name="counter")
    data = json.loads(resp.content)
    assert resp.status_code == 200
    assert data["name"] == "counter"
    assert data["value"] == 99


def test_urlpatterns_get_node_not_found() -> None:
    _setup()
    g = Graph("test")
    patterns = graphrefly_urlpatterns(g)
    view = _find_view(patterns, "graphrefly-node")

    request = rf.get("/nodes/missing")
    resp = view(request, name="missing")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# URL patterns — PUT /nodes/<name>
# ---------------------------------------------------------------------------


def test_urlpatterns_set_node() -> None:
    _setup()
    g = Graph("test")
    g.add("counter", state(0))

    patterns = graphrefly_urlpatterns(g)
    view = _find_view(patterns, "graphrefly-node")

    request = rf.put(
        "/nodes/counter/",
        data=json.dumps({"value": 42}),
        content_type="application/json",
    )
    resp = view(request, name="counter")
    data = json.loads(resp.content)
    assert resp.status_code == 200
    assert data["ok"] is True
    assert g.get("counter") == 42


def test_urlpatterns_set_node_not_found() -> None:
    _setup()
    g = Graph("test")
    patterns = graphrefly_urlpatterns(g)
    view = _find_view(patterns, "graphrefly-node")

    request = rf.put(
        "/nodes/missing/",
        data=json.dumps({"value": 1}),
        content_type="application/json",
    )
    resp = view(request, name="missing")
    assert resp.status_code == 404


def test_urlpatterns_set_node_invalid_json() -> None:
    _setup()
    g = Graph("test")
    g.add("x", state(0))
    patterns = graphrefly_urlpatterns(g)
    view = _find_view(patterns, "graphrefly-node")

    request = rf.put("/nodes/x/", data="not json", content_type="application/json")
    resp = view(request, name="x")
    assert resp.status_code == 400


def test_urlpatterns_set_node_missing_value_key() -> None:
    _setup()
    g = Graph("test")
    g.add("x", state(0))
    patterns = graphrefly_urlpatterns(g)
    view = _find_view(patterns, "graphrefly-node")

    request = rf.put(
        "/nodes/x/",
        data=json.dumps({"wrong": 1}),
        content_type="application/json",
    )
    resp = view(request, name="x")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# URL patterns — GET /describe
# ---------------------------------------------------------------------------


def test_urlpatterns_describe() -> None:
    _setup()
    g = Graph("test")
    g.add("a", state(1))
    g.add("b", derived([g.node("a")], lambda deps, _: deps[0] * 2))
    patterns = graphrefly_urlpatterns(g)
    view = _find_view(patterns, "graphrefly-describe")

    request = rf.get("/describe/")
    resp = view(request)
    data = json.loads(resp.content)
    assert resp.status_code == 200
    assert "nodes" in data


# ---------------------------------------------------------------------------
# URL patterns — GET /snapshot
# ---------------------------------------------------------------------------


def test_urlpatterns_snapshot() -> None:
    _setup()
    g = Graph("test")
    g.add("x", state(7))
    patterns = graphrefly_urlpatterns(g)
    view = _find_view(patterns, "graphrefly-snapshot")

    request = rf.get("/snapshot/")
    resp = view(request)
    data = json.loads(resp.content)
    assert resp.status_code == 200
    assert data["name"] == "test"


# ---------------------------------------------------------------------------
# URL patterns — actor_resolver
# ---------------------------------------------------------------------------


def test_urlpatterns_actor_resolver() -> None:
    _setup()
    g = Graph("test")
    g.add("x", state(0))

    resolved_actors: list[Any] = []

    def my_actor_resolver(request: Any) -> dict[str, Any]:
        actor = {"type": "human", "id": "user-1"}
        resolved_actors.append(actor)
        return actor

    patterns = graphrefly_urlpatterns(g, actor_resolver=my_actor_resolver)
    view = _find_view(patterns, "graphrefly-describe")

    request = rf.get("/describe/")
    resp = view(request)
    assert resp.status_code == 200
    assert len(resolved_actors) == 1
    assert resolved_actors[0]["id"] == "user-1"


# ---------------------------------------------------------------------------
# URL patterns — prefix
# ---------------------------------------------------------------------------


def test_urlpatterns_prefix() -> None:
    _setup()
    g = Graph("test")
    g.add("x", state(5))
    patterns = graphrefly_urlpatterns(g, prefix="api/v1/")
    # Verify the URL patterns have the prefix.
    for p in patterns:
        assert str(p.pattern).startswith("api/v1/")


# ---------------------------------------------------------------------------
# URL patterns — registry-based (no explicit graph)
# ---------------------------------------------------------------------------


def test_urlpatterns_registry_based() -> None:
    _setup()
    g = Graph("test")
    g.add("x", state(42))
    register_graph(g)

    patterns = graphrefly_urlpatterns()
    view = _find_view(patterns, "graphrefly-node")

    request = rf.get("/nodes/x")
    resp = view(request, name="x")
    data = json.loads(resp.content)
    assert resp.status_code == 200
    assert data["value"] == 42


# ---------------------------------------------------------------------------
# SSE response helper
# ---------------------------------------------------------------------------


def test_sse_response_from_node() -> None:
    from django.http import StreamingHttpResponse

    s = state(0)
    resp = sse_response(s)
    assert isinstance(resp, StreamingHttpResponse)
    assert resp["Content-Type"] == "text/event-stream"
    s.unsubscribe()


# ---------------------------------------------------------------------------
# Observe SSE — single node
# ---------------------------------------------------------------------------


def test_observe_node_sse() -> None:
    """SSE stream from a single node emits data events."""
    _setup()
    g = Graph("test")
    s = state(0)
    g.add("x", s)

    patterns = graphrefly_urlpatterns(g)
    view = _find_view(patterns, "graphrefly-observe-node")

    request = rf.get("/observe/x/")
    resp = view(request, name="x")

    # Subscription is registered during view() — emit from a background
    # thread so the main thread can iterate streaming_content.
    ready = threading.Event()

    def _emit() -> None:
        ready.wait(timeout=5)
        g.set("x", 42)
        s.down([(MessageType.COMPLETE,)])

    t = threading.Thread(target=_emit, daemon=True)
    t.start()

    text = ""
    for chunk in resp.streaming_content:
        # Signal the emitter once the iterator is consuming.
        ready.set()
        text += chunk if isinstance(chunk, str) else chunk.decode()
        if "event: complete" in text:
            break

    t.join(timeout=2)
    assert "event: data" in text
    assert "42" in text


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


def test_middleware_attaches_registry() -> None:
    _setup()
    g = Graph("test")
    g.add("x", state(1))
    register_graph(g)

    captured: list[Any] = [None]

    def fake_view(request: Any) -> Any:
        captured[0] = request.graphrefly
        from django.http import HttpResponse

        return HttpResponse("ok")

    middleware = GraphReflyMiddleware(fake_view)
    request = rf.get("/")
    middleware(request)

    assert captured[0] is not None
    assert "default" in captured[0]
    assert captured[0]["default"] is g


# ---------------------------------------------------------------------------
# Reactive round-trip: set via PUT, read derived via GET
# ---------------------------------------------------------------------------


def test_reactive_round_trip() -> None:
    _setup()
    g = Graph("test")
    g.add("a", state(1))
    b = derived([g.node("a")], lambda deps, _: deps[0] * 10)
    g.add("b", b)

    # Subscribe so the derived node is live.
    unsub = b.subscribe(lambda _msgs: None)

    patterns = graphrefly_urlpatterns(g)
    view = _find_view(patterns, "graphrefly-node")

    # Read initial derived value.
    resp = view(rf.get("/nodes/b"), name="b")
    assert json.loads(resp.content)["value"] == 10

    # Set upstream node.
    set_req = rf.put(
        "/nodes/a/",
        data=json.dumps({"value": 5}),
        content_type="application/json",
    )
    view(set_req, name="a")

    # Derived updates synchronously through the reactive graph.
    resp2 = view(rf.get("/nodes/b"), name="b")
    assert json.loads(resp2.content)["value"] == 50

    unsub()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_view(patterns: list[Any], name: str) -> Any:
    """Find a view function by URL pattern name."""
    for p in patterns:
        if p.name == name:
            return p.callback
    raise ValueError(f"URL pattern {name!r} not found")
