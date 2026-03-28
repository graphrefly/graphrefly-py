"""Graph container — roadmap Phase 1.1 (GRAPHREFLY-SPEC §3.1–3.3)."""

from __future__ import annotations

import pytest

from graphrefly import Graph, MessageType, node


def test_graph_add_get_node_set() -> None:
    g = Graph("g")
    a = node(initial=1)
    g.add("a", a)
    assert g.node("a") is a
    assert g.get("a") == 1
    g.set("a", 2)
    assert g.get("a") == 2
    assert a.get() == 2


def test_graph_add_sets_name_when_unset() -> None:
    g = Graph("g")
    a = node(initial=0)
    assert a.name is None
    g.add("x", a)
    assert a.name == "x"


def test_graph_add_duplicate_name() -> None:
    g = Graph("g")
    g.add("a", node(initial=1))
    with pytest.raises(KeyError):
        g.add("a", node(initial=2))


def test_graph_add_same_instance_twice() -> None:
    g = Graph("g")
    a = node(initial=1)
    g.add("a", a)
    with pytest.raises(ValueError, match="already registered"):
        g.add("b", a)


def test_graph_remove_sends_teardown_to_subscribers() -> None:
    g = Graph("g")
    src = node(initial=0)
    flat: list = []

    def sink(msgs: object) -> None:
        for m in msgs:
            flat.append(m[0])

    g.add("src", src)
    unsub = src.subscribe(sink)
    g.remove("src")
    unsub()
    assert MessageType.TEARDOWN in flat


def test_graph_remove_clears_incident_edges() -> None:
    g = Graph("g")
    a = node(initial=1)
    b = node([a], lambda deps, _: deps[0])
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")
    g.remove("a")
    assert g.edges() == frozenset()
    with pytest.raises(KeyError):
        g.node("a")


def test_graph_connect_requires_dep_edge() -> None:
    g = Graph("g")
    a = node(initial=1)
    b = node([a], lambda deps, _: deps[0] * 2)
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")
    assert g.edges() == frozenset({("a", "b")})


def test_graph_connect_rejects_missing_dep() -> None:
    g = Graph("g")
    a = node(initial=1)
    b = node(initial=2)
    g.add("a", a)
    g.add("b", b)
    with pytest.raises(ValueError, match="dependency list"):
        g.connect("a", "b")


def test_graph_connect_self_loop() -> None:
    g = Graph("g")
    a = node(initial=1)
    g.add("a", a)
    with pytest.raises(ValueError, match="itself"):
        g.connect("a", "a")


def test_graph_disconnect_removes_edge_record() -> None:
    g = Graph("g")
    a = node(initial=1)
    b = node([a], lambda deps, _: deps[0])
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")
    g.disconnect("a", "b")
    assert g.edges() == frozenset()


def test_graph_unknown_node() -> None:
    g = Graph("g")
    with pytest.raises(KeyError):
        g.node("nope")


def test_graph_connect_unknown_node_raises_keyerror() -> None:
    g = Graph("g")
    a = node(initial=1)
    g.add("a", a)
    with pytest.raises(KeyError, match="unknown node"):
        g.connect("a", "missing")
    with pytest.raises(KeyError, match="unknown node"):
        g.connect("missing", "a")


def test_graph_disconnect_without_connect_raises() -> None:
    g = Graph("g")
    a = node(initial=1)
    b = node([a], lambda deps, _: deps[0])
    g.add("a", a)
    g.add("b", b)
    with pytest.raises(ValueError, match="no registered edge"):
        g.disconnect("a", "b")


def test_graph_connect_idempotent() -> None:
    g = Graph("g")
    a = node(initial=1)
    b = node([a], lambda deps, _: deps[0])
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")
    g.connect("a", "b")
    assert g.edges() == frozenset({("a", "b")})
    g.disconnect("a", "b")
    assert g.edges() == frozenset()
    with pytest.raises(ValueError, match="no registered edge"):
        g.disconnect("a", "b")


def test_graph_name_must_be_non_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Graph("")
