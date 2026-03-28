"""Graph container — roadmap Phase 1.1–1.3 (GRAPHREFLY-SPEC §3.1–3.6)."""

from __future__ import annotations

import pytest

from graphrefly import GRAPH_META_SEGMENT, PATH_SEP, Graph, MessageType, node


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


def test_mount_resolve_qualified_path() -> None:
    parent = Graph("parent")
    child = Graph("child")
    a = node(initial=42)
    child.add("a", a)
    parent.mount("sub", child)
    assert parent.resolve("sub::a") is a
    assert parent.node("sub::a") is a
    assert parent.get("sub::a") == 42


def test_connect_delegates_to_child_for_same_owner() -> None:
    parent = Graph("parent")
    child = Graph("child")
    a = node(initial=1)
    b = node([a], lambda deps, _: deps[0] * 2)
    child.add("a", a)
    child.add("b", b)
    parent.mount("sub", child)
    parent.connect("sub::a", "sub::b")
    assert child.edges() == frozenset({("a", "b")})
    assert parent.edges() == frozenset()


def test_connect_cross_subgraph_on_parent_edges() -> None:
    parent = Graph("parent")
    child = Graph("child")
    root = node(initial=10)
    inner = node([root], lambda deps, _: deps[0] + 1)
    parent.add("root", root)
    child.add("inner", inner)
    parent.mount("sub", child)
    parent.connect("root", "sub::inner")
    assert parent.edges() == frozenset({("root", "sub::inner")})


def test_signal_propagates_to_mounted_nodes() -> None:
    parent = Graph("parent")
    child = Graph("child")
    a = node(initial=0)
    seen: list = []
    child.add("a", a)
    parent.mount("sub", child)
    unsub = a.subscribe(lambda msgs: seen.extend(msgs))
    parent.signal([(MessageType.PAUSE,)])
    unsub()
    assert any(m[0] == MessageType.PAUSE for m in seen)


def test_remove_mount_teardowns_child_nodes() -> None:
    parent = Graph("parent")
    child = Graph("child")
    a = node(initial=0)
    flat: list = []

    def sink(msgs: object) -> None:
        for m in msgs:
            flat.append(m[0])

    child.add("a", a)
    parent.mount("sub", child)
    unsub = a.subscribe(sink)
    parent.remove("sub")
    unsub()
    assert MessageType.TEARDOWN in flat


def test_mount_cycle_rejected() -> None:
    g1 = Graph("g1")
    g2 = Graph("g2")
    g1.mount("two", g2)
    with pytest.raises(ValueError, match="cycle"):
        g2.mount("back", g1)


def test_mount_self_rejected() -> None:
    g = Graph("g")
    with pytest.raises(ValueError, match="itself"):
        g.mount("s", g)


def test_add_rejects_double_colon_in_name() -> None:
    g = Graph("g")
    with pytest.raises(ValueError, match="path separator"):
        g.add("bad::name", node(initial=0))


def test_single_colon_in_names_allowed() -> None:
    g = Graph("g")
    a = node(initial=5)
    g.add("my:node", a)
    assert g.node("my:node") is a
    assert g.get("my:node") == 5
    g.set("my:node", 10)
    assert g.get("my:node") == 10


def test_graph_name_with_single_colon_allowed() -> None:
    g = Graph("app:v2")
    assert g.name == "app:v2"


def test_graph_name_with_double_colon_rejected() -> None:
    with pytest.raises(ValueError, match="must not contain"):
        Graph("a::b")


def test_mount_name_collides_with_node() -> None:
    parent = Graph("parent")
    child = Graph("child")
    parent.add("sub", node(initial=1))
    with pytest.raises(KeyError, match="already a registered node"):
        parent.mount("sub", child)


def test_add_rejects_name_used_as_mount() -> None:
    parent = Graph("parent")
    child = Graph("child")
    parent.mount("sub", child)
    with pytest.raises(KeyError, match="mounted subgraph"):
        parent.add("sub", node(initial=1))


def test_single_colon_in_mount_name_allowed() -> None:
    root = Graph("r")
    child = Graph("c")
    child.add("x", node(initial=0))
    root.mount("my:mount", child)
    assert root.resolve("my:mount::x").get() == 0


def test_resolve_strips_leading_graph_name() -> None:
    root = Graph("app")
    child = Graph("pay")
    n = node(initial=1)
    child.add("x", n)
    root.mount("payment", child)
    assert root.resolve("app::payment::x") is n


def test_resolve_on_child_strips_graph_name() -> None:
    child = Graph("pay")
    n = node(initial=2)
    child.add("x", n)
    assert child.resolve("pay::x") is n
    assert child.resolve("x") is n


def test_node_get_set_accept_qualified_paths() -> None:
    root = Graph("app")
    child = Graph("sub")
    n = node(initial=10)
    child.add("val", n)
    root.mount("sub", child)
    assert root.node("sub::val") is n
    assert root.get("sub::val") == 10
    root.set("sub::val", 42)
    assert root.get("sub::val") == 42


def test_remove_mount_prunes_cross_subgraph_edges() -> None:
    root = Graph("root")
    child = Graph("child")
    a = node(initial=0)
    b = node([a], lambda deps, _: deps[0])
    root.add("a", a)
    child.add("b", b)
    root.mount("sub", child)
    root.connect("a", "sub::b")
    assert len(root.edges()) == 1
    root.remove("sub")
    assert root.edges() == frozenset()


def test_same_child_mounted_twice_rejected() -> None:
    root = Graph("root")
    child = Graph("ch")
    child.add("n", node(initial=0))
    root.mount("c1", child)
    with pytest.raises(ValueError, match="already mounted"):
        root.mount("c2", child)


def test_signal_visits_mounts_before_local_nodes() -> None:
    root = Graph("root")
    child = Graph("child")
    order: list[str] = []
    root_node = node(initial=0)
    child_node = node(initial=0)
    child.add("cn", child_node)
    root.add("rn", root_node)
    root.mount("c", child)
    child_node.subscribe(lambda _msgs: order.append("child"))
    root_node.subscribe(lambda _msgs: order.append("root"))
    root.signal([(MessageType.PAUSE,)])
    assert order == ["child", "root"]


def test_reserved_meta_segment_rejected_for_add_and_mount() -> None:
    g = Graph("g")
    with pytest.raises(ValueError, match="reserved"):
        g.add(GRAPH_META_SEGMENT, node(initial=0))
    with pytest.raises(ValueError, match="reserved"):
        g.mount(GRAPH_META_SEGMENT, Graph("c"))


def test_resolve_meta_path_requires_key_after_segment() -> None:
    g = Graph("g")
    n = node(initial=1, meta={"tag": 0})
    g.add("a", n)
    incomplete = f"a{PATH_SEP}{GRAPH_META_SEGMENT}"
    with pytest.raises(ValueError, match="meta path requires"):
        g.resolve(incomplete)


def test_resolve_meta_path_and_get_set() -> None:
    g = Graph("g")
    n = node(initial=1, meta={"tag": "hello"})
    g.add("a", n)
    meta_path = f"a{PATH_SEP}{GRAPH_META_SEGMENT}{PATH_SEP}tag"
    assert g.node(meta_path) is n.meta["tag"]
    assert g.get(meta_path) == "hello"
    g.set(meta_path, "bye")
    assert g.get(meta_path) == "bye"


def test_connect_rejects_meta_path() -> None:
    g = Graph("g")
    a = node(initial=1, meta={"m": 0})
    b = node([a], lambda deps, _: deps[0])
    g.add("a", a)
    g.add("b", b)
    mp = f"a{PATH_SEP}{GRAPH_META_SEGMENT}{PATH_SEP}m"
    with pytest.raises(ValueError, match="meta paths"):
        g.connect(mp, "b")


def test_signal_reaches_meta_companions() -> None:
    g = Graph("g")
    n = node(initial=0, meta={"m": 0})
    g.add("a", n)
    meta_path = f"a{PATH_SEP}{GRAPH_META_SEGMENT}{PATH_SEP}m"
    seen: list = []
    g.node(meta_path).subscribe(lambda msgs: seen.extend(m[0] for m in msgs))
    g.signal([(MessageType.PAUSE,)])
    assert MessageType.PAUSE in seen


def test_signal_teardown_not_duplicated_to_meta() -> None:
    g = Graph("g")
    n = node(initial=0, meta={"m": 0})
    g.add("a", n)
    meta_path = f"a{PATH_SEP}{GRAPH_META_SEGMENT}{PATH_SEP}m"
    teardowns = 0

    def sink(msgs: object) -> None:
        nonlocal teardowns
        for m in msgs:
            if m[0] == MessageType.TEARDOWN:
                teardowns += 1

    g.node(meta_path).subscribe(sink)
    g.signal([(MessageType.TEARDOWN,)])
    assert teardowns == 1


def test_describe_includes_nodes_edges_subgraphs_and_meta_paths() -> None:
    parent = Graph("parent")
    child = Graph("child")
    a = node(initial=1, meta={"desc": "x"})
    b = node([a], lambda deps, _: deps[0] * 2)
    parent.add("a", a)
    parent.add("b", b)
    parent.connect("a", "b")
    child.add("x", node(initial=0))
    parent.mount("sub", child)
    d = parent.describe()
    assert d["name"] == "parent"
    assert "a" in d["nodes"] and "b" in d["nodes"]
    meta_k = f"a{PATH_SEP}{GRAPH_META_SEGMENT}{PATH_SEP}desc"
    assert meta_k in d["nodes"]
    assert d["nodes"]["b"]["deps"] == ["a"]
    assert {"from": "a", "to": "b"} in d["edges"]
    assert "sub" in d["subgraphs"]
    assert f"sub{PATH_SEP}x" in d["nodes"]


def test_observe_single_vs_all() -> None:
    g = Graph("g")
    a = node(initial=0)
    b = node(initial=0)
    g.add("a", a)
    g.add("b", b)
    single: list = []
    unsub = g.observe("a").subscribe(lambda msgs: single.extend(msgs))
    a.down([(MessageType.DATA, 7)])
    unsub()
    assert any(m[0] == MessageType.DATA for m in single)

    all_rows: list[tuple[str, object]] = []
    unsub_all = g.observe().subscribe(lambda path, msgs: all_rows.append((path, msgs[0][0])))
    b.down([(MessageType.DATA, 3)])
    unsub_all()
    assert ("b", MessageType.DATA) in all_rows
