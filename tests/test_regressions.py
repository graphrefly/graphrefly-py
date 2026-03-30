# Regression tests for spec-verified behaviors.
# Each test names the bug and anchors it to a spec section.
# Format: test_<stable_bug_name> with # Spec: GRAPHREFLY-SPEC §x.x comment

from __future__ import annotations

from typing import Any

import pytest

from graphrefly import Graph, state
from graphrefly.core import MessageType, derived, node
from graphrefly.core.sugar import pipe
from graphrefly.extra.tier1 import combine, map
from graphrefly.extra.tier2 import switch_map


# ---------------------------------------------------------------------------
# 1. RESOLVED transitive skip — downstream fn must not rerun
# ---------------------------------------------------------------------------


def test_resolved_transitive_skip_does_not_rerun_downstream() -> None:
    """RESOLVED from a mid node prevents the leaf fn from running at all.

    Regression: derived fn was spuriously called even when a dep emitted RESOLVED
    (value unchanged), violating the transitive-skip guarantee.

    # Spec: GRAPHREFLY-SPEC §1.3.3
    """
    source = state(1)
    # Mid node: maps to "pos"/"other"; emits RESOLVED when output unchanged
    mid = pipe(source, map(lambda x: "pos" if x > 0 else "other", equals=lambda a, b: a == b))
    leaf_runs = 0

    def leaf_fn(deps: list, _a: object) -> str:
        nonlocal leaf_runs
        leaf_runs += 1
        return deps[0].upper()

    leaf = derived([mid], leaf_fn)
    leaf.subscribe(lambda _m: None)
    assert leaf_runs == 1  # initial compute
    before = leaf_runs
    # Push a different positive value — mid output stays "pos" → RESOLVED
    source.down([(MessageType.DATA, 2)])
    after = leaf_runs
    assert after == before, (
        f"leaf fn ran {after - before} extra time(s) after mid emitted RESOLVED; "
        "transitive skip violated (GRAPHREFLY-SPEC §1.3.3)"
    )


# ---------------------------------------------------------------------------
# 2. Diamond recompute count through operators
# ---------------------------------------------------------------------------


def test_diamond_recompute_count_through_operators() -> None:
    """combine on a shared source recomputes the leaf exactly once per upstream change.

    Regression: in a diamond topology (A → B, A → C, combine(B,C) → D), D was
    recomputing twice when A changed because DIRTY/DATA arrived from both branches
    before proper bitmask settlement.

    # Spec: GRAPHREFLY-SPEC §2 (diamond resolution)
    """
    a = state(0)
    b = pipe(a, map(lambda x: x + 1))
    c = pipe(a, map(lambda x: x * 2))
    leaf_runs = 0

    def leaf_fn(deps: list[Any], _a: object) -> int:
        nonlocal leaf_runs
        leaf_runs += 1
        return deps[0] + deps[1]

    d = derived([b, c], leaf_fn)
    d.subscribe(lambda _m: None)
    before = leaf_runs
    a.down([(MessageType.DIRTY,), (MessageType.DATA, 5)])
    after = leaf_runs
    assert after - before == 1, (
        f"leaf recomputed {after - before} time(s) for one upstream change; "
        "expected exactly 1 (GRAPHREFLY-SPEC §2 diamond resolution)"
    )
    assert d.get() == 6 + 10  # (5+1) + (5*2)


# ---------------------------------------------------------------------------
# 3. describe() output validates against Appendix B schema
# ---------------------------------------------------------------------------

_VALID_TYPES = {"state", "derived", "producer", "operator", "effect"}
_VALID_STATUSES = {"disconnected", "dirty", "settled", "resolved", "completed", "errored"}


def _validate_appendix_b(data: dict) -> None:
    """Assert ``data`` conforms to the Appendix B JSON schema shape.

    Checks: required top-level keys, node field types, enum values for 'type'
    and 'status', edge shape, and subgraphs list.

    # Spec: GRAPHREFLY-SPEC Appendix B
    """
    assert isinstance(data, dict), "describe() must return a dict"
    for key in ("name", "nodes", "edges"):
        assert key in data, f"Missing required key '{key}' in describe() output"
    assert isinstance(data["name"], str), "'name' must be a string"
    assert isinstance(data["nodes"], dict), "'nodes' must be an object"
    assert isinstance(data["edges"], list), "'edges' must be an array"
    assert isinstance(data.get("subgraphs", []), list), "'subgraphs' must be an array"

    for path, spec in data["nodes"].items():
        assert isinstance(spec, dict), f"Node entry for '{path}' must be an object"
        assert "type" in spec, f"Node '{path}' missing required 'type'"
        assert "status" in spec, f"Node '{path}' missing required 'status'"
        assert spec["type"] in _VALID_TYPES, (
            f"Node '{path}' has invalid type '{spec['type']}'; "
            f"expected one of {_VALID_TYPES}"
        )
        assert spec["status"] in _VALID_STATUSES, (
            f"Node '{path}' has invalid status '{spec['status']}'; "
            f"expected one of {_VALID_STATUSES}"
        )
        if "deps" in spec:
            assert isinstance(spec["deps"], list), f"Node '{path}' 'deps' must be an array"
            for dep in spec["deps"]:
                assert isinstance(dep, str), f"Node '{path}' dep item must be a string"
        if "meta" in spec:
            assert isinstance(spec["meta"], dict), f"Node '{path}' 'meta' must be an object"

    for edge in data["edges"]:
        assert isinstance(edge, dict), "Each edge must be an object"
        assert "from" in edge and "to" in edge, "Edge must have 'from' and 'to' keys"
        assert isinstance(edge["from"], str), "Edge 'from' must be a string"
        assert isinstance(edge["to"], str), "Edge 'to' must be a string"

    for sub in data.get("subgraphs", []):
        assert isinstance(sub, str), "Each subgraph entry must be a string"


def test_describe_matches_appendix_b_schema() -> None:
    """describe() output on a representative graph satisfies the Appendix B JSON schema.

    Regression: describe() was omitting required fields or using invalid enum values,
    making the output non-conformant with the published spec schema.

    # Spec: GRAPHREFLY-SPEC Appendix B
    """
    g = Graph("payment_flow")
    retry_limit = state(3, meta={"description": "Max retry attempts", "access": "both"})
    inp = state("raw", name="input")
    validate = derived(
        [inp],
        lambda deps, _: bool(deps[0]),
        name="validate",
        meta={"description": "Validates payment data"},
    )
    g.add("retry_limit", retry_limit)
    g.add("input", inp)
    g.add("validate", validate)
    g.connect("input", "validate")
    validate.subscribe(lambda _m: None)

    description = g.describe()
    _validate_appendix_b(description)

    # Extra spot-checks for this specific graph
    assert description["name"] == "payment_flow"
    node_keys = set(description["nodes"].keys())
    assert "retry_limit" in node_keys
    assert "input" in node_keys
    assert "validate" in node_keys
    assert {"from": "input", "to": "validate"} in description["edges"]


# ---------------------------------------------------------------------------
# 4. forward_inner initial attach must not duplicate DATA
# ---------------------------------------------------------------------------


def test_switch_map_forward_inner_does_not_duplicate_derived_initial_data() -> None:
    """switch_map emits a single initial DATA when inner emits during subscribe.

    Regression: _forward_inner used subscribe + get() without an emitted guard,
    which could duplicate initial DATA for derived inners.

    # Spec: GRAPHREFLY-SPEC §2 (dynamic inner subscription via operators)
    """
    outer = state(0)
    base = state(10)
    derived_inner = derived([base], lambda deps, _a: deps[0] + 1)
    out = pipe(outer, switch_map(lambda _v: derived_inner))
    seen: list[tuple[MessageType, Any]] = []

    def sink(batch: list[tuple[MessageType, Any] | tuple[MessageType]]) -> None:
        for m in batch:
            if len(m) > 1:
                seen.append((m[0], m[1]))
            else:
                seen.append((m[0], None))

    out.subscribe(sink)
    values = [payload for t, payload in seen if t is MessageType.DATA]
    assert values.count(11) == 1, (
        f"Expected one initial DATA(11), got {values.count(11)} in {values}; "
        "this indicates forward_inner duplicate initial emission."
    )
