"""Patterns orchestration tests (roadmap 4.1 initial slice)."""

from __future__ import annotations

from graphrefly import Graph, state
from graphrefly.patterns import orchestration


def test_pipeline_returns_graph() -> None:
    g = orchestration.pipeline("wf")
    assert isinstance(g, Graph)
    assert g.name == "wf"


def test_task_registers_step_and_wires_edges() -> None:
    g = orchestration.pipeline("wf")
    source = state(1)
    g.add("input", source)
    doubled = orchestration.task(
        g,
        "double",
        lambda deps, _a: deps[0] * 2,
        deps=["input"],
    )
    doubled.subscribe(lambda _msgs: None)
    g.set("input", 3)
    assert g.get("double") == 6
    assert ("input", "double") in g.edges()


def test_task_with_node_dep_still_records_explicit_edge() -> None:
    g = orchestration.pipeline("wf")
    source = state(2)
    g.add("input", source)
    orchestration.task(g, "double_by_ref", lambda deps, _a: deps[0] * 2, deps=[source])
    assert ("input", "double_by_ref") in g.edges()


def test_branch_emits_then_else_labels() -> None:
    g = orchestration.pipeline("wf")
    source = state(1)
    g.add("input", source)
    routed = orchestration.branch(g, "route", "input", lambda value: value >= 2)
    seen: list[str] = []

    def sink(msgs: object) -> None:
        for msg in msgs:
            if msg[0].value == "DATA":
                seen.append(msg[1]["branch"])

    routed.subscribe(sink)
    g.set("input", 2)
    assert "else" in seen
    assert "then" in seen


def test_task_and_branch_describe_as_derived_with_canonical_meta() -> None:
    g = orchestration.pipeline("wf")
    source = state(1)
    g.add("input", source)
    orchestration.task(g, "t", lambda deps, _a: deps[0], deps=["input"])
    orchestration.branch(g, "b", "input", lambda value: value > 0)
    desc = g.describe()
    t_key = next((k for k in desc["nodes"] if k == "t" or k.endswith("::t")), None)
    b_key = next((k for k in desc["nodes"] if k == "b" or k.endswith("::b")), None)
    assert t_key is not None
    assert b_key is not None
    assert desc["nodes"][t_key]["type"] == "derived"
    assert desc["nodes"][b_key]["type"] == "derived"
    assert desc["nodes"][t_key]["meta"]["orchestration_type"] == "task"
    assert desc["nodes"][b_key]["meta"]["orchestration_type"] == "branch"


def test_gate_and_approval_hold_value_when_closed() -> None:
    g = orchestration.pipeline("wf")
    source = state(1)
    opened = state(True)
    approved = state(True)
    g.add("input", source)
    g.add("open", opened)
    g.add("approved", approved)
    gated = orchestration.gate(g, "gated", "input", "open")
    reviewed = orchestration.approval(g, "reviewed", gated, "approved")
    gated.subscribe(lambda _msgs: None)
    reviewed.subscribe(lambda _msgs: None)

    g.set("input", 2)
    assert g.get("reviewed") == 2
    g.set("open", False)
    g.set("input", 3)
    assert g.get("reviewed") == 2
    g.set("open", True)
    g.set("approved", False)
    g.set("input", 4)
    assert g.get("reviewed") == 3
