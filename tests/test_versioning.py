"""Tests for node versioning (GRAPHREFLY-SPEC §7) — V0 and V1."""

from graphrefly import Graph, batch, derived, describe_node, node, state
from graphrefly.core.protocol import MessageType
from graphrefly.core.versioning import (
    V0,
    V1,
    advance_version,
    create_versioning,
    default_hash,
    is_v1,
)

# ---------------------------------------------------------------------------
# Unit: versioning module
# ---------------------------------------------------------------------------


class TestCreateVersioning:
    def test_v0_returns_id_and_version_0(self):
        v = create_versioning(0)
        assert isinstance(v, V0)
        assert isinstance(v.id, str)
        assert len(v.id) > 0
        assert v.version == 0
        assert not is_v1(v)

    def test_v0_accepts_custom_id(self):
        v = create_versioning(0, id="custom-id")
        assert v.id == "custom-id"

    def test_v1_returns_id_version_cid_prev(self):
        v = create_versioning(1, 42)
        assert isinstance(v, V1)
        assert v.version == 0
        assert is_v1(v)
        assert isinstance(v.cid, str)
        assert len(v.cid) == 16
        assert v.prev is None

    def test_v1_cid_deterministic_for_same_value(self):
        a = create_versioning(1, {"x": 1, "y": 2})
        b = create_versioning(1, {"y": 2, "x": 1})  # different key order
        assert a.cid == b.cid

    def test_v1_cid_differs_for_different_values(self):
        a = create_versioning(1, 42)
        b = create_versioning(1, 43)
        assert a.cid != b.cid


class TestAdvanceVersion:
    def test_v0_increments_version(self):
        v = create_versioning(0)
        advance_version(v, 1, default_hash)
        assert v.version == 1
        advance_version(v, 2, default_hash)
        assert v.version == 2

    def test_v1_increments_version_and_rotates_cid(self):
        v = create_versioning(1, "hello")
        assert isinstance(v, V1)
        first_cid = v.cid
        advance_version(v, "world", default_hash)
        assert v.version == 1
        assert v.prev == first_cid
        assert v.cid != first_cid


class TestDefaultHash:
    def test_produces_16_hex_chars(self):
        h = default_hash(42)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self):
        assert default_hash([1, 2, 3]) == default_hash([1, 2, 3])

    def test_sorts_object_keys(self):
        assert default_hash({"b": 2, "a": 1}) == default_hash({"a": 1, "b": 2})


# ---------------------------------------------------------------------------
# Integration: node with versioning
# ---------------------------------------------------------------------------


class TestNodeV0Versioning:
    def test_state_node_has_v(self):
        s = state(42, versioning=0)
        assert s.v is not None
        assert isinstance(s.v.id, str)
        assert s.v.version == 0

    def test_version_increments_on_data(self):
        s = state(0, versioning=0)
        unsub = s.subscribe(lambda msgs: None)
        s.down([(MessageType.DATA, 1)])
        assert s.v.version == 1
        s.down([(MessageType.DATA, 2)])
        assert s.v.version == 2
        unsub()

    def test_version_no_increment_on_resolved(self):
        s = state(0, versioning=0)
        unsub = s.subscribe(lambda msgs: None)
        s.down([(MessageType.DIRTY,), (MessageType.RESOLVED,)])
        assert s.v.version == 0
        unsub()

    def test_version_increments_in_derived(self):
        a = state(1, name="a")
        b = derived([a], lambda deps, _: deps[0] * 2, name="b", versioning=0)
        unsub = b.subscribe(lambda msgs: None)
        # Initial compute on subscribe: version is already 1
        assert b.v.version == 1
        a.down([(MessageType.DIRTY,), (MessageType.DATA, 5)])
        assert b.v.version == 2
        assert b.get() == 10
        unsub()

    def test_version_no_increment_when_derived_unchanged(self):
        a = state(1, name="a")
        # derived always returns constant
        b = derived([a], lambda deps, _: 42, name="b", versioning=0)
        unsub = b.subscribe(lambda msgs: None)
        # Initial compute: version 0 → 1
        assert b.v.version == 1
        a.down([(MessageType.DIRTY,), (MessageType.DATA, 2)])
        # Value unchanged → RESOLVED, no version bump
        assert b.v.version == 1
        unsub()

    def test_v_is_none_when_not_enabled(self):
        s = state(0)
        assert s.v is None

    def test_custom_id(self):
        s = state(0, versioning=0, versioning_id="my-node-001")
        assert s.v.id == "my-node-001"


class TestNodeV1Versioning:
    def test_state_node_has_cid_and_prev(self):
        s = state(42, versioning=1)
        assert s.v is not None
        assert is_v1(s.v)
        assert isinstance(s.v.cid, str)
        assert len(s.v.cid) == 16
        assert s.v.prev is None

    def test_cid_changes_and_prev_links(self):
        s = state("a", versioning=1)
        initial_cid = s.v.cid

        unsub = s.subscribe(lambda msgs: None)
        s.down([(MessageType.DATA, "b")])
        assert s.v.version == 1
        assert s.v.cid != initial_cid
        assert s.v.prev == initial_cid
        unsub()

    def test_linked_history(self):
        s = state(0, versioning=1)
        unsub = s.subscribe(lambda msgs: None)
        cids = [s.v.cid]
        for i in range(1, 4):
            s.down([(MessageType.DATA, i)])
            cids.append(s.v.cid)
            assert s.v.prev == cids[i - 1]
        # All cids unique
        assert len(set(cids)) == 4
        unsub()

    def test_custom_hash_function(self):
        def custom_hash(v):
            return f"custom-{v}"

        s = state(42, versioning=1, versioning_hash=custom_hash)
        assert s.v.cid == "custom-42"

        unsub = s.subscribe(lambda msgs: None)
        s.down([(MessageType.DATA, 99)])
        assert s.v.cid == "custom-99"
        assert s.v.prev == "custom-42"
        unsub()


class TestVersioningInBatch:
    def test_version_advances_per_data_in_batch(self):
        s = state(0, versioning=0)
        unsub = s.subscribe(lambda msgs: None)
        with batch():
            s.down([(MessageType.DATA, 1)])
            s.down([(MessageType.DATA, 2)])
        assert s.v.version == 2
        assert s.get() == 2
        unsub()


class TestVersioningInDescribe:
    def test_v0_in_describe(self):
        s = state(0, versioning=0, name="x")
        d = describe_node(s)
        assert "v" in d
        assert d["v"]["id"] == s.v.id
        assert d["v"]["version"] == 0
        assert "cid" not in d["v"]

    def test_v1_in_describe(self):
        s = state(42, versioning=1, name="x")
        d = describe_node(s)
        assert "v" in d
        assert isinstance(d["v"]["cid"], str)
        assert d["v"]["prev"] is None

    def test_no_v_when_disabled(self):
        s = state(0, name="x")
        d = describe_node(s)
        assert "v" not in d


class TestVersioningEffect:
    def test_effect_v0_version_stays_0(self):
        a = state(1)
        called = []
        # Use node() directly since effect() sugar doesn't accept extra opts
        e = node([a], lambda deps, _: called.append(1), describe_kind="effect", versioning=0)
        unsub = e.subscribe(lambda msgs: None)
        # Effect runs on initial connect
        a.down([(MessageType.DIRTY,), (MessageType.DATA, 2)])
        # Effect runs again
        assert len(called) == 2
        # Effects don't emit DATA, so version doesn't advance
        assert e.v.version == 0
        unsub()


class TestApplyVersioning:
    def test_retroactively_adds_v0_to_node_without_versioning(self):
        s = state(42)
        assert s.v is None
        s._apply_versioning(0)
        assert s.v is not None
        assert s.v.version == 0
        assert isinstance(s.v.id, str)

    def test_noop_when_versioning_already_enabled(self):
        s = state(42, versioning=0, versioning_id="keep-me")
        s._apply_versioning(0, id="overwrite")
        assert s.v is not None
        assert s.v.id == "keep-me"

    def test_retroactively_adds_v1_with_cid(self):
        s = state("hello")
        s._apply_versioning(1)
        assert s.v is not None
        assert is_v1(s.v)
        assert isinstance(s.v.cid, str)


class TestGraphVersioning:
    def test_set_versioning_retroactively_applies_to_existing_nodes(self):
        g = Graph("test")
        a = state(1, name="a")
        b = state(2, name="b")
        g.add("a", a)
        g.add("b", b)
        assert a.v is None
        assert b.v is None

        g.set_versioning(0)

        assert a.v is not None
        assert a.v.version == 0
        assert b.v is not None

    def test_set_versioning_applies_to_newly_added_nodes(self):
        g = Graph("test")
        g.set_versioning(0)
        c = state(3, name="c")
        assert c.v is None
        g.add("c", c)
        assert c.v is not None

    def test_graph_diff_skips_value_compare_when_versions_match(self):
        node_a = {
            "type": "state",
            "status": "settled",
            "value": {"big": "object"},
            "deps": [],
            "meta": {},
            "v": {"id": "x", "version": 5},
        }
        a = {"name": "g", "nodes": {"n": node_a}, "edges": [], "subgraphs": []}
        b = {"name": "g", "nodes": {"n": dict(node_a)}, "edges": [], "subgraphs": []}
        result = Graph.diff(a, b)
        assert result.nodesChanged == []
