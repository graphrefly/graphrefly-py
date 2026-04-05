"""Graph container — Phase 1.1–1.4 + 1.6 acceptance (GRAPHREFLY-SPEC §3.1–3.8)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from graphrefly import (
    GRAPH_META_SEGMENT,
    GRAPH_SNAPSHOT_VERSION,
    PATH_SEP,
    Graph,
    MessageType,
    batch,
    derived,
    node,
    reachable,
    state,
)


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
    d = parent.describe(detail="standard")
    assert d["name"] == "parent"
    assert "a" in d["nodes"] and "b" in d["nodes"]
    meta_k = f"a{PATH_SEP}{GRAPH_META_SEGMENT}{PATH_SEP}desc"
    assert meta_k in d["nodes"]
    assert d["nodes"]["b"]["deps"] == ["a"]
    assert {"from": "a", "to": "b"} in d["edges"]
    assert "sub" in d["subgraphs"]
    assert f"sub{PATH_SEP}x" in d["nodes"]


def test_describe_filter_supports_deps_includes_meta_has_and_path_predicate() -> None:
    g = Graph("g")
    a = state(1, meta={"label": "input"})
    b = derived([a], lambda deps, _: deps[0] + 1)
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")

    by_deps = g.describe(detail="standard", filter={"deps_includes": "a"})
    assert list(by_deps["nodes"].keys()) == ["b"]

    by_meta = g.describe(detail="standard", filter={"meta_has": "label"})
    assert "a" in by_meta["nodes"]

    by_path = g.describe(
        detail="standard",
        filter=lambda p, d: p.startswith("a") and d["type"] == "state",
    )
    assert "a" in by_path["nodes"]


def test_meta_has_filter_at_minimal_detail_excludes_all_nodes() -> None:
    """metaHas filter at minimal detail excludes all nodes (no meta at minimal)."""
    g = Graph("g")
    g.add("a", state(1, meta={"label": "input"}))
    # Default (minimal) detail omits meta, so meta_has filter matches nothing
    d = g.describe(filter={"meta_has": "label"})
    assert list(d["nodes"].keys()) == []
    g.destroy()


def test_reachable_upstream_uses_deps_and_incoming_edges() -> None:
    described = {
        "name": "g",
        "nodes": {
            "a": {"type": "state", "status": "settled", "deps": [], "meta": {}},
            "b": {"type": "derived", "status": "settled", "deps": ["a"], "meta": {}},
            "c": {"type": "derived", "status": "settled", "deps": ["b"], "meta": {}},
            "x": {"type": "state", "status": "settled", "deps": [], "meta": {}},
        },
        "edges": [{"from": "x", "to": "b"}],
        "subgraphs": [],
    }
    assert reachable(described, "c", "upstream") == ["a", "b", "x"]
    assert reachable(described, "c", "upstream", max_depth=1) == ["b"]


def test_reachable_downstream_uses_reverse_deps_and_outgoing_edges() -> None:
    described = {
        "name": "g",
        "nodes": {
            "a": {"type": "state", "status": "settled", "deps": [], "meta": {}},
            "b": {"type": "derived", "status": "settled", "deps": ["a"], "meta": {}},
            "c": {"type": "derived", "status": "settled", "deps": ["b"], "meta": {}},
            "sink": {"type": "state", "status": "settled", "deps": [], "meta": {}},
        },
        "edges": [{"from": "a", "to": "sink"}],
        "subgraphs": [],
    }
    assert reachable(described, "a", "downstream") == ["b", "c", "sink"]
    assert reachable(described, "a", "downstream", max_depth=1) == ["b", "sink"]


def test_reachable_validates_direction_and_max_depth_int() -> None:
    described = {
        "name": "g",
        "nodes": {"a": {"type": "state", "status": "settled", "deps": [], "meta": {}}},
        "edges": [],
        "subgraphs": [],
    }
    with pytest.raises(ValueError, match="direction must be"):
        reachable(described, "a", "sideways")
    with pytest.raises(ValueError, match="int >= 0"):
        reachable(described, "a", "upstream", max_depth=1.5)
    with pytest.raises(ValueError, match="int >= 0"):
        reachable(described, "a", "upstream", max_depth=True)
    with pytest.raises(ValueError, match="int >= 0"):
        reachable(described, "a", "upstream", max_depth=-1)
    assert reachable(described, "a", "upstream", max_depth=0) == []


def test_reachable_handles_unknown_start_and_malformed_payload_defensively() -> None:
    malformed = {
        "name": "g",
        "nodes": {"a": {"type": "state", "status": "settled", "deps": ["b"], "meta": {}}},
        "edges": [{"from": "b", "to": "a"}, {"from": 1}, None],
        "subgraphs": [],
    }
    assert reachable(malformed, "missing", "upstream") == []
    assert (
        reachable(
            {"name": "g", "nodes": None, "edges": None, "subgraphs": []},
            "a",
            "upstream",
        )
        == []
    )


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


def test_observe_timeline_includes_timestamp_and_batch_context() -> None:
    g = Graph("g")
    a = state(0)
    b = derived([a], lambda deps, _: deps[0] + 1)
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")
    obs = g.observe("b", timeline=True)
    with batch():
        g.set("a", 2)
    obs.dispose()
    assert any("timestamp_ns" in e for e in obs.events)
    assert any(e.get("in_batch") is True for e in obs.events)


def test_observe_causal_and_derived_capture_trigger_and_dep_values() -> None:
    g = Graph("g")
    a = state(0)
    b = derived([a], lambda deps, _: deps[0] + 1)
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")
    obs = g.observe("b", causal=True, derived=True, timeline=True)
    g.set("a", 5)
    obs.dispose()
    derived_events = [e for e in obs.events if e.get("type") == "derived"]
    derived_event = derived_events[-1] if derived_events else None
    assert derived_event is not None
    assert derived_event.get("dep_values") == [5]
    data_events = [e for e in obs.events if e.get("type") == "data"]
    data_event = data_events[-1] if data_events else None
    assert data_event is not None
    assert data_event.get("trigger_dep_index") == 0
    assert data_event.get("trigger_dep_name") == "a"
    assert data_event.get("dep_values") == [5]


def test_observe_causal_includes_trigger_version_when_dep_has_v0() -> None:
    g = Graph("g")
    a = state(0, versioning=0)
    b = derived([a], lambda deps, _: deps[0] + 1)
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")
    obs = g.observe("b", causal=True, derived=True, timeline=True)
    g.set("a", 5)
    obs.dispose()
    data_events = [e for e in obs.events if e.get("type") == "data"]
    data_event = data_events[-1] if data_events else None
    assert data_event is not None
    assert data_event.get("trigger_version") == {"id": a.v.id, "version": a.v.version}


def test_observe_derived_includes_initial_run_without_trigger_dep_index() -> None:
    g = Graph("g")
    a = state(0)
    b = derived([a], lambda deps, _: deps[0] + 1)
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")
    obs = g.observe("b", causal=True, derived=True, timeline=True)
    g.set("a", 3)
    obs.dispose()
    derived_events = [e for e in obs.events if e.get("type") == "derived"]
    assert len(derived_events) >= 2
    assert any(e.get("dep_values") == [0] for e in derived_events)
    assert any(e.get("dep_values") == [3] for e in derived_events)


def test_observe_error_does_not_mark_completed_cleanly() -> None:
    g = Graph("g")
    a = node(initial=0)
    g.add("a", a)
    obs = g.observe("a", timeline=True)
    a.down([(MessageType.ERROR, RuntimeError("boom"))])
    a.down([(MessageType.COMPLETE,)])
    obs.dispose()
    assert obs.errored is True
    assert obs.completed_cleanly is False


def test_to_mermaid_exports_qualified_nodes_edges_and_direction() -> None:
    g = Graph("g")
    child = Graph("child")
    a = state(0)
    b = derived([a], lambda deps, _: deps[0] + 1)
    child.add("a", a)
    child.add("b", b)
    child.connect("a", "b")
    g.mount("sub", child)
    text = g.to_mermaid(direction="TD")
    assert "flowchart TD" in text
    assert '["sub::a"]' in text
    assert '["sub::b"]' in text
    assert "-->" in text


def test_to_d2_exports_nodes_edges_and_direction_mapping() -> None:
    g = Graph("g")
    a = state(1)
    b = derived([a], lambda deps, _: deps[0] * 2)
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")
    text = g.to_d2(direction="RL")
    assert "direction: left" in text
    assert '"a"' in text
    assert '"b"' in text
    assert "->" in text


def test_to_mermaid_rejects_invalid_direction() -> None:
    g = Graph("g")
    g.add("a", state(0))
    with pytest.raises(ValueError, match="invalid diagram direction"):
        g.to_mermaid(direction="SIDEWAYS")


def test_to_d2_rejects_invalid_direction() -> None:
    g = Graph("g")
    g.add("a", state(0))
    with pytest.raises(ValueError, match="invalid diagram direction"):
        g.to_d2(direction="SIDEWAYS")


def test_annotate_resolves_existing_path() -> None:
    g = Graph("g")
    g.add("a", state(0))
    g.annotate("a", "human reviewed")
    entries = g.trace_log()
    assert entries
    assert entries[-1].path == "a"
    assert entries[-1].reason == "human reviewed"
    with pytest.raises(KeyError):
        g.annotate("missing", "nope")


def test_trace_log_returns_empty_when_inspector_disabled() -> None:
    g = Graph("g")
    g.add("a", state(0))
    g.annotate("a", "first")
    original = Graph.inspector_enabled
    try:
        Graph.inspector_enabled = False
        assert g.trace_log() == []
        g.annotate("a", "second")
    finally:
        Graph.inspector_enabled = original
    # Existing entries remain stored and visible once re-enabled.
    assert any(entry.reason == "first" for entry in g.trace_log())
    assert not any(entry.reason == "second" for entry in g.trace_log())


def test_spy_logs_with_filters_and_dispose() -> None:
    g = Graph("g")
    g.add("a", state(0))
    lines: list[str] = []
    spy = g.spy(
        "a",
        include_types=["data", "dirty", "resolved"],
        exclude_types=["resolved"],
        theme="none",
        logger=lambda line, _event: lines.append(line),
    )
    assert hasattr(spy, "result")
    g.set("a", 1)
    spy.dispose()
    assert any("DATA" in line for line in lines)
    assert not any("RESOLVED" in line for line in lines)


def test_spy_json_graph_wide() -> None:
    g = Graph("g")
    g.add("a", state(0))
    g.add("b", state(0))
    lines: list[str] = []
    spy = g.spy(
        format="json",
        theme="none",
        logger=lambda line, _event: lines.append(line),
    )
    assert hasattr(spy, "result")
    g.set("a", 2)
    g.set("b", 3)
    spy.dispose()
    parsed = [json.loads(line) for line in lines]
    assert any(evt.get("type") == "data" and evt.get("path") == "a" for evt in parsed)
    assert any(evt.get("type") == "data" and evt.get("path") == "b" for evt in parsed)


def test_dump_graph_pretty_and_json() -> None:
    g = Graph("g")
    a = state(1)
    b = derived([a], lambda deps, _: deps[0] + 1)
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")
    pretty = g.dump_graph(format="pretty")
    assert "Graph g" in pretty
    assert "Nodes:" in pretty
    assert "Edges:" in pretty
    json_text = g.dump_graph(format="json", indent=2)
    assert json_text == g.dump_graph(format="json", indent=2)
    payload = json.loads(json_text)
    assert payload["name"] == "g"
    assert "a" in payload["nodes"]
    assert payload["edges"] == [{"from": "a", "to": "b"}]


def test_destroy_teardowns_and_clears_registry() -> None:
    g = Graph("g")
    a = node(initial=0)
    seen: list = []
    g.add("a", a)
    unsub = a.subscribe(lambda msgs: seen.extend(m[0] for m in msgs))
    g.destroy()
    unsub()
    assert MessageType.TEARDOWN in seen
    with pytest.raises(KeyError):
        g.node("a")


def test_destroy_recurses_into_mounts() -> None:
    root = Graph("root")
    child = Graph("child")
    a = node(initial=0)
    flat: list = []
    child.add("a", a)
    root.mount("sub", child)
    unsub = a.subscribe(lambda msgs: flat.extend(m[0] for m in msgs))
    root.destroy()
    unsub()
    assert MessageType.TEARDOWN in flat
    with pytest.raises(KeyError):
        child.node("a")


def test_snapshot_envelope_and_sorted_nodes() -> None:
    g = Graph("g")
    g.add("z", state(1))
    g.add("a", state(2))
    s = g.snapshot()
    assert s["version"] == GRAPH_SNAPSHOT_VERSION
    assert list(s["nodes"]) == ["a", "z"]


def test_to_json_is_deterministic() -> None:
    g = Graph("g")
    g.add("b", state(2))
    g.add("a", state(1))
    j1 = g.to_json()
    j2 = g.to_json()
    assert j1 == j2
    parsed = json.loads(j1)
    assert list(parsed["nodes"]) == ["a", "b"]


def test_restore_applies_values() -> None:
    g = Graph("g")
    g.add("x", state(10))
    snap = g.snapshot()
    g.set("x", 99)
    g.restore(snap)
    assert g.get("x") == 10


def test_restore_rejects_snapshot_when_nodes_not_dict() -> None:
    g = Graph("g")
    bad = {
        "version": GRAPH_SNAPSHOT_VERSION,
        "name": "g",
        "nodes": [],
        "edges": [],
        "subgraphs": [],
    }
    with pytest.raises(TypeError, match="nodes"):
        g.restore(bad)


def test_from_snapshot_round_trip_flat_state() -> None:
    g = Graph("app")
    g.add("n", state(42, meta={"tag": "x"}))
    s = g.snapshot()
    g2 = Graph.from_snapshot(s)
    assert g2.name == "app"
    assert g2.get("n") == 42
    meta_p = f"n{PATH_SEP}{GRAPH_META_SEGMENT}{PATH_SEP}tag"
    assert g2.get(meta_p) == "x"


def test_from_snapshot_round_trip_with_mount() -> None:
    root = Graph("root")
    child = Graph("child")
    child.add("v", state(7))
    root.mount("sub", child)
    s = root.snapshot()
    r2 = Graph.from_snapshot(s)
    assert r2.get("sub::v") == 7


def test_from_snapshot_rejects_edges() -> None:
    g = Graph("g")
    a = state(1)
    b = derived([a], lambda deps, _: deps[0] * 2)
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")
    snap = g.snapshot()
    assert snap["edges"]
    with pytest.raises(ValueError, match="could not reconstruct"):
        Graph.from_snapshot(snap)


def test_from_snapshot_rejects_non_state_nodes() -> None:
    snap = {
        "version": GRAPH_SNAPSHOT_VERSION,
        "name": "g",
        "nodes": {
            "x": {"type": "derived", "status": "settled", "value": 1, "deps": [], "meta": {}}
        },
        "edges": [],
        "subgraphs": [],
    }
    with pytest.raises(ValueError, match="could not reconstruct"):
        Graph.from_snapshot(snap)


def test_restore_skips_derived_and_recomputes() -> None:
    """Restore applies state values; derived recomputes from deps (parity with TS)."""
    g = Graph("g")
    a = state(10)
    b = derived([a], lambda deps, _: deps[0] * 2)
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")
    b.subscribe(lambda _: None)  # activate
    snap = g.snapshot()
    g.set("a", 0)
    assert g.get("b") == 0
    g.restore(snap)
    assert g.get("a") == 10
    assert g.get("b") == 20


def test_restore_rejects_name_mismatch() -> None:
    g = Graph("one")
    snap = {
        "version": GRAPH_SNAPSHOT_VERSION,
        "name": "other",
        "nodes": {},
        "edges": [],
        "subgraphs": [],
    }
    with pytest.raises(ValueError, match="other"):
        g.restore(snap)


def test_restore_sets_meta_companion_from_snapshot() -> None:
    n0 = node(initial=0, meta={"tag": "hi"})
    g0 = Graph("g")
    g0.add("n", n0)
    snap = g0.snapshot()
    meta_path = f"n{PATH_SEP}{GRAPH_META_SEGMENT}{PATH_SEP}tag"

    n1 = node(initial=0, meta={"tag": ""})
    g1 = Graph("g")
    g1.add("n", n1)
    g1.restore(snap)
    assert g1.get(meta_path) == "hi"


def test_from_snapshot_with_build_restores_derived() -> None:
    """from_snapshot with build callback can handle graphs with edges (parity with TS)."""
    a = state(0)
    g0 = Graph("app")
    g0.add("a", a)
    g0.set("a", 7)
    snap = g0.snapshot()

    def build(g: Graph) -> None:
        g.add("a", state(0))

    g1 = Graph.from_snapshot(snap, build=build)
    assert g1.get("a") == 7


def test_auto_checkpoint_triggers_only_for_message_tier_gte_2() -> None:
    class Adapter:
        def __init__(self) -> None:
            self.saved: list[dict[str, Any]] = []

        def save(self, data: dict[str, Any]) -> None:
            self.saved.append(data)

    g = Graph("g")
    g.add("a", state(0))
    ad = Adapter()
    handle = g.auto_checkpoint(ad, debounce_ms=10, compact_every=2)
    g.signal([(MessageType.PAUSE, "lock")])
    time.sleep(0.03)
    assert len(ad.saved) == 0
    g.set("a", 1)
    time.sleep(0.03)
    assert len(ad.saved) == 1
    handle.dispose()


def test_restore_only_pattern_selective_hydration() -> None:
    g = Graph("g")
    g.add("a", state(1))
    g.add("b", state(2))
    snap = g.snapshot()
    g.set("a", 10)
    g.set("b", 20)
    g.restore(snap, only="a")
    assert g.get("a") == 1
    assert g.get("b") == 20


def test_from_snapshot_reconstructs_dynamic_nodes_via_factory_registry() -> None:
    g0 = Graph("g")
    a = state(1)
    total = derived([a], lambda deps, _: deps[0] + 1)
    g0.add("a", a)
    g0.add("sum", total)
    g0.connect("a", "sum")
    total.subscribe(lambda _msgs: None)
    snap = g0.snapshot()

    def sum_factory(name: str, ctx: dict[str, Any]) -> Any:
        return derived(list(ctx["resolved_deps"]), lambda deps, _: deps[0] + 1, name=name)

    Graph.register_factory("sum", sum_factory)
    try:
        g1 = Graph.from_snapshot(snap)
        g1.node("sum").subscribe(lambda _msgs: None)
        g1.set("a", 5)
        assert g1.get("sum") == 6
    finally:
        Graph.unregister_factory("sum")


def test_to_json_has_trailing_newline() -> None:
    g = Graph("g")
    g.add("a", state(0))
    j = g.to_json()
    assert j.endswith("\n")


# --- Phase 1.6 — explicit acceptance tests (roadmap) ---


def test_graph_set_records_last_mutation() -> None:
    g = Graph("g")
    n = state(0)
    g.add("n", n)
    actor = {"type": "human", "id": "u1"}
    g.set("n", 5, actor=actor)
    lm = n.last_mutation
    assert lm is not None
    assert lm.actor["type"] == "human"
    assert lm.actor["id"] == "u1"
    assert lm.timestamp_ns > 0


def test_graph_set_qualified_mount_path_records_last_mutation_on_inner_node() -> None:
    root = Graph("root")
    child = Graph("child")
    inner = state(0)
    child.add("x", inner)
    root.mount("sub", child)
    root.set("sub::x", 9, actor={"type": "wallet", "id": "0x1"})
    lm = inner.last_mutation
    assert lm is not None
    assert lm.actor["type"] == "wallet"
    assert lm.actor["id"] == "0x1"


def test_internal_down_preserves_last_mutation_after_graph_set() -> None:
    """Internal ``down`` skips attribution; prior write record stays (spec / Phase 1.5)."""
    g = Graph("g")
    n = state(0)
    g.add("n", n)
    g.set("n", 1, actor={"type": "human", "id": "h1"})
    recorded = n.last_mutation
    assert recorded is not None
    n.down([(MessageType.DATA, 2)], internal=True)
    assert n.last_mutation == recorded
    assert n.get() == 2


def test_describe_output_has_expected_top_level_and_node_shape() -> None:
    """Validate describe() shape including Appendix B enum values."""
    g = Graph("g")
    a = state(1)
    b = derived([a], lambda d, _: d[0] + 1)
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")
    d = g.describe(detail="standard")
    for key in ("name", "nodes", "edges", "subgraphs"):
        assert key in d
    assert d["name"] == "g"
    assert isinstance(d["nodes"], dict)
    assert isinstance(d["edges"], list)
    assert isinstance(d["subgraphs"], list)
    valid_types = {"state", "derived", "producer", "operator", "effect"}
    valid_statuses = {"disconnected", "dirty", "settled", "resolved", "completed", "errored"}
    for _path, spec in d["nodes"].items():
        assert isinstance(spec, dict)
        assert {"type", "status", "deps", "meta"} <= spec.keys()
        assert spec["type"] in valid_types, f"bad type: {spec['type']}"
        assert spec["status"] in valid_statuses, f"bad status: {spec['status']}"
        assert isinstance(spec["deps"], list)
        assert isinstance(spec["meta"], dict)
    for e in d["edges"]:
        assert set(e) == {"from", "to"}


def test_observe_graph_wide_reports_qualified_paths_on_data() -> None:
    g = Graph("g")
    a = state(1)
    b = state(2)
    g.add("a", a)
    g.add("b", b)
    data_paths: set[str] = set()

    def sink(p: str, msgs: object) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                data_paths.add(p)

    unsub = g.observe().subscribe(sink)
    g.set("a", 10)
    g.set("b", 20)
    unsub()
    assert data_paths == {"a", "b"}


def test_observe_mount_prefix_in_graph_wide_mode() -> None:
    root = Graph("root")
    child = Graph("child")
    n = state(0)
    child.add("v", n)
    root.mount("sub", child)
    seen: set[str] = set()

    def sink(p: str, msgs: object) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                seen.add(p)

    unsub = root.observe().subscribe(sink)
    root.set("sub::v", 7)
    unsub()
    assert seen == {"sub::v"}


def test_from_snapshot_round_trip_matches_to_json() -> None:
    g = Graph("app")
    g.add("z", state(3))
    g.add("a", state(1))
    snap = g.snapshot()
    g2 = Graph.from_snapshot(snap)
    assert g2.to_json() == g.to_json()


def test_observe_derived_sees_dirty_before_data() -> None:
    """observe(path) on a derived node sees DIRTY before DATA on upstream set (parity with TS)."""
    g = Graph("g")
    a = state(0)
    b = derived([a], lambda deps, _: deps[0] + 1)
    g.add("a", a)
    g.add("b", b)
    g.connect("a", "b")
    seq: list[MessageType] = []
    unsub = g.observe("b").subscribe(lambda msgs: seq.extend(m[0] for m in msgs))
    g.set("a", 5)
    unsub()
    i_dirty = next((i for i, t in enumerate(seq) if t is MessageType.DIRTY), -1)
    i_data = next((i for i, t in enumerate(seq) if t is MessageType.DATA), -1)
    assert i_dirty >= 0, "expected DIRTY in observe stream"
    assert i_data > i_dirty, "expected DATA after DIRTY"
    assert g.get("b") == 6


def test_snapshot_json_wire_round_trip_with_mount() -> None:
    """Serialize snapshot to JSON text, parse back, and restore with mounted subgraph."""
    root = Graph("app")
    child = Graph("sub")
    child.add("x", state(42, meta={"tag": "hi"}))
    root.mount("sub", child)
    snap_json = root.to_json()
    parsed = json.loads(snap_json)

    def build(g: Graph) -> None:
        ch = Graph("sub")
        ch.add("x", state(0, meta={"tag": ""}))
        g.mount("sub", ch)

    restored = Graph.from_snapshot(parsed, build=build)
    assert restored.name == "app"
    assert restored.get("sub::x") == 42
    meta_path = f"x{PATH_SEP}{GRAPH_META_SEGMENT}{PATH_SEP}tag"
    assert restored.get(f"sub::{meta_path}") == "hi"


def test_signal_reaches_sibling_mounts() -> None:
    """Signal from root graph reaches nodes in two sibling mounted subgraphs."""
    root = Graph("root")
    c1 = Graph("c1")
    c2 = Graph("c2")
    n1 = state(0)
    n2 = state(0)
    c1.add("n1", n1)
    c2.add("n2", n2)
    root.mount("m1", c1)
    root.mount("m2", c2)
    pauses: list[str] = []
    n1.subscribe(lambda msgs: pauses.extend("n1" for m in msgs if m[0] is MessageType.PAUSE))
    n2.subscribe(lambda msgs: pauses.extend("n2" for m in msgs if m[0] is MessageType.PAUSE))
    root.signal([(MessageType.PAUSE, "k")])
    assert sorted(pauses) == ["n1", "n2"]


# ---------------------------------------------------------------------------
# Phase 3.3b — Progressive disclosure for describe() and observe()
# ---------------------------------------------------------------------------


class TestDescribeDetailLevels:
    """describe() detail level support (3.3b)."""

    def test_default_minimal_returns_type_and_deps_only(self) -> None:
        g = Graph("detail")
        a = state(10, meta={"label": "A"})
        b = derived([a], lambda deps, _: deps[0] * 2)
        g.add("a", a)
        g.add("b", b)
        d = g.describe()
        na = d["nodes"]["a"]
        assert na["type"] == "state"
        assert na["deps"] == []
        assert "status" not in na
        assert "value" not in na
        assert "meta" not in na
        nb = d["nodes"]["b"]
        assert nb["type"] == "derived"
        assert nb["deps"] == ["a"]
        assert "status" not in nb
        g.destroy()

    def test_standard_includes_status_value_meta(self) -> None:
        g = Graph("std")
        a = state(10, meta={"label": "A"})
        b = derived([a], lambda deps, _: deps[0] * 2)
        g.add("a", a)
        g.add("b", b)
        b.subscribe(lambda _: None)  # connect b
        d = g.describe(detail="standard")
        na = d["nodes"]["a"]
        assert na["status"] == "settled"
        assert na["value"] == 10
        assert na["meta"]["label"] == "A"
        assert "v" not in na  # no versioning
        nb = d["nodes"]["b"]
        assert nb["status"] == "settled"
        assert nb["value"] == 20
        g.destroy()

    def test_full_includes_versioning(self) -> None:
        from graphrefly import policy

        g = Graph("full")
        tester = {"type": "human", "id": "t1", "name": "tester"}
        a = state(
            5,
            versioning=0,
            meta={"desc": "x"},
            guard=policy(lambda allow, deny: [allow("write"), allow("observe")]),
        )
        g.add("a", a)
        # Trigger a mutation with actor to populate last_mutation
        a.down([(MessageType.DATA, 6)], actor=tester)
        d = g.describe(detail="full")
        na = d["nodes"]["a"]
        assert na["status"] == "settled"
        assert na["value"] == 6
        assert "v" in na
        assert na["v"]["version"] >= 1
        assert "guard" in na
        assert "last_mutation" in na
        assert na["last_mutation"]["actor"]["name"] == "tester"
        g.destroy()


class TestDescribeFieldSelection:
    """describe() GraphQL-style field selection (3.3b)."""

    def test_fields_override_detail(self) -> None:
        g = Graph("fields")
        g.add("x", state(42, meta={"label": "X"}))
        d = g.describe(fields=["type", "status"])
        nx = d["nodes"]["x"]
        assert nx["type"] == "state"
        assert nx["status"] == "settled"
        assert "value" not in nx
        assert "meta" not in nx
        g.destroy()

    def test_dotted_meta_path(self) -> None:
        g = Graph("dot")
        g.add("x", state(1, meta={"label": "L", "secret": "S"}))
        d = g.describe(fields=["type", "meta.label"])
        nx = d["nodes"]["x"]
        assert nx["type"] == "state"
        assert nx["meta"] == {"label": "L"}
        assert "value" not in nx
        g.destroy()

    def test_fields_takes_precedence_over_detail(self) -> None:
        g = Graph("prec")
        g.add("x", state(1))
        d = g.describe(detail="full", fields=["type"])
        assert "status" not in d["nodes"]["x"]
        g.destroy()


class TestDescribeFormatSpec:
    """describe(format='spec') returns minimal GraphSpec output (3.3b)."""

    def test_spec_format_is_minimal(self) -> None:
        g = Graph("spec")
        a = state(1, meta={"desc": "source"})
        b = derived([a], lambda deps, _: deps[0] + 1)
        g.add("a", a)
        g.add("b", b)
        d = g.describe(format="spec")
        assert d["nodes"]["a"]["type"] == "state"
        assert "status" not in d["nodes"]["a"]
        assert "value" not in d["nodes"]["a"]
        assert "meta" not in d["nodes"]["a"]
        assert d["nodes"]["b"]["deps"] == ["a"]
        g.destroy()


class TestDescribeExpand:
    """describe().expand() re-reads the live graph with higher detail (3.3b)."""

    def test_expand_from_minimal_to_standard(self) -> None:
        g = Graph("expand")
        a = state(1, meta={"label": "A"})
        g.add("a", a)
        minimal = g.describe()
        assert "value" not in minimal["nodes"]["a"]
        g.set("a", 99)
        expanded = minimal.expand("standard")
        assert expanded["nodes"]["a"]["value"] == 99
        assert expanded["nodes"]["a"]["status"] == "settled"
        g.destroy()

    def test_expand_with_field_list(self) -> None:
        g = Graph("expf")
        g.add("x", state(5))
        d = g.describe()
        expanded = d.expand(["type", "value"])
        assert expanded["nodes"]["x"]["value"] == 5
        assert "status" not in expanded["nodes"]["x"]
        g.destroy()


class TestObserveDetailLevels:
    """observe() detail levels (3.3b)."""

    def test_minimal_only_data_events(self) -> None:
        Graph.inspector_enabled = True
        g = Graph("obs-min")
        a = state(1)
        b = derived([a], lambda deps, _: deps[0] * 2)
        g.add("a", a)
        g.add("b", b)
        obs = g.observe("b", detail="minimal")
        g.set("a", 2)
        assert all(e["type"] == "data" for e in obs.events)
        assert obs.dirty_count >= 1  # tracked internally
        assert obs.values["b"] == 4
        obs.dispose()
        g.destroy()
        Graph.inspector_enabled = False

    def test_full_enables_timeline_causal_derived(self) -> None:
        Graph.inspector_enabled = True
        g = Graph("obs-full")
        a = state(10)
        b = derived([a], lambda deps, _: deps[0] * 2)
        g.add("a", a)
        g.add("b", b)
        obs = g.observe("b", detail="full")
        g.set("a", 20)
        data_events = [e for e in obs.events if e["type"] == "data"]
        assert len(data_events) >= 2  # initial + update
        last = data_events[-1]
        assert last["data"] == 40
        assert "timestamp_ns" in last  # timeline
        assert last.get("trigger_dep_name") == "a"  # causal
        derived_events = [e for e in obs.events if e["type"] == "derived"]
        assert len(derived_events) >= 1
        assert derived_events[-1]["dep_values"] == [20]
        obs.dispose()
        g.destroy()
        Graph.inspector_enabled = False

    def test_graph_wide_minimal(self) -> None:
        Graph.inspector_enabled = True
        g = Graph("obs-all-min")
        a = state(1)
        b = derived([a], lambda deps, _: deps[0])
        g.add("a", a)
        g.add("b", b)
        obs = g.observe(detail="minimal")
        g.set("a", 2)
        assert all(e["type"] == "data" for e in obs.events)
        assert obs.dirty_count >= 1
        obs.dispose()
        g.destroy()
        Graph.inspector_enabled = False


class TestObserveExpand:
    """observe().expand() resubscribes with higher detail (3.3b)."""

    def test_expand_minimal_to_full(self) -> None:
        Graph.inspector_enabled = True
        g = Graph("obs-exp")
        a = state(1)
        b = derived([a], lambda deps, _: deps[0] + 10)
        g.add("a", a)
        g.add("b", b)
        minimal = g.observe("b", detail="minimal")
        g.set("a", 2)
        assert all(e["type"] == "data" for e in minimal.events)
        full = minimal.expand("full")
        g.set("a", 3)
        data_events = [e for e in full.events if e["type"] == "data"]
        assert len(data_events) >= 1
        assert data_events[-1].get("timestamp_ns") is not None
        assert data_events[-1].get("trigger_dep_name") == "a"
        full.dispose()
        g.destroy()
        Graph.inspector_enabled = False
