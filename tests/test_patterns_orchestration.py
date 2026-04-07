"""Patterns orchestration tests (roadmap 4.1 initial slice)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from graphrefly import Graph, state
from graphrefly.core.protocol import MessageType
from graphrefly.patterns import orchestration
from graphrefly.patterns.orchestration import GateController, gate


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
                seen.append(msg[1].branch)

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
    desc = g.describe(detail="standard")
    t_key = next((k for k in desc["nodes"] if k == "t" or k.endswith("::t")), None)
    b_key = next((k for k in desc["nodes"] if k == "b" or k.endswith("::b")), None)
    assert t_key is not None
    assert b_key is not None
    assert desc["nodes"][t_key]["type"] == "derived"
    assert desc["nodes"][b_key]["type"] == "derived"
    assert desc["nodes"][t_key]["meta"]["orchestration_type"] == "task"
    assert desc["nodes"][b_key]["meta"]["orchestration_type"] == "branch"


def test_valve_and_approval_hold_value_when_closed() -> None:
    g = orchestration.pipeline("wf")
    source = state(1)
    opened = state(True)
    approved = state(True)
    g.add("input", source)
    g.add("open", opened)
    g.add("approved", approved)
    gated = orchestration.valve(g, "gated", "input", "open")
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


def test_for_each_runs_side_effects_and_forwards() -> None:
    g = orchestration.pipeline("wf")
    source = state(1)
    g.add("input", source)
    seen: list[int] = []
    sink = orchestration.for_each(g, "sink", "input", lambda value, _a: seen.append(int(value)))
    sink.subscribe(lambda _msgs: None)
    g.set("input", 2)
    g.set("input", 3)
    assert seen == [2, 3]


def test_join_emits_latest_tuple() -> None:
    g = orchestration.pipeline("wf")
    a = state(1)
    b = state("x")
    g.add("a", a)
    g.add("b", b)
    joined = orchestration.join(g, "j", ["a", "b"])
    joined.subscribe(lambda _msgs: None)
    g.set("a", 5)
    assert g.get("j") == (5, "x")


def test_loop_applies_iterate_fixed_times() -> None:
    g = orchestration.pipeline("wf")
    source = state(2)
    g.add("input", source)
    looped = orchestration.loop(
        g,
        "pow2x3",
        "input",
        lambda value, _i, _a: int(value) * 2,
        iterations=3,
    )
    looped.subscribe(lambda _msgs: None)
    assert g.get("pow2x3") == 16


def test_sub_pipeline_mounts_child_graph() -> None:
    root = orchestration.pipeline("root")
    child = orchestration.sub_pipeline(
        root,
        "child",
        lambda sub: sub.add("n", state(1)),
    )
    assert isinstance(child, Graph)
    assert root.get("child::n") == 1


def test_sensor_controls_emit_values() -> None:
    g = orchestration.pipeline("wf")
    s = orchestration.sensor(g, "s")
    seen: list[int] = []

    def sink(msgs: object) -> None:
        for msg in msgs:
            if msg[0].value == "DATA":
                seen.append(int(msg[1]))

    s.node.subscribe(sink)
    s.push(10)
    s.push(20)
    assert seen == [10, 20]


def test_wait_delays_data_forwarding() -> None:
    g = orchestration.pipeline("wf")
    source = state(1)
    g.add("input", source)
    delayed = orchestration.wait(g, "delayed", "input", 0.02)
    delayed.subscribe(lambda _msgs: None)
    g.set("input", 2)
    assert g.get("delayed") == 1
    time.sleep(0.05)
    assert g.get("delayed") == 2


def test_on_failure_recovers_from_errors() -> None:
    g = orchestration.pipeline("wf")
    src = state(1)
    g.add("src", src)

    def run(deps: list[object], _a: object) -> object:
        if int(deps[0]) > 1:
            raise ValueError("boom")
        return deps[0]

    failing = orchestration.task(
        g,
        "failing",
        run,
        deps=["src"],
    )
    recovered = orchestration.on_failure(g, "recovered", failing, lambda _err, _a: 999)
    recovered.subscribe(lambda _msgs: None)
    g.set("src", 2)
    assert g.get("recovered") == 999


def test_for_each_stops_user_callback_after_terminal_error() -> None:
    g = orchestration.pipeline("wf")
    src = state(0)
    g.add("src", src)
    seen: list[int] = []

    def run(value: object, _a: object) -> None:
        seen.append(int(value))
        if int(value) >= 1:
            raise ValueError("stop")

    sink = orchestration.for_each(g, "sink", "src", run)
    sink.subscribe(lambda _msgs: None)
    g.set("src", 1)
    g.set("src", 2)
    assert seen == [1]


def test_wait_cancels_pending_timers_on_teardown() -> None:
    g = orchestration.pipeline("wf")
    src = state(1)
    g.add("input", src)
    delayed = orchestration.wait(g, "delayed", "input", 0.02)
    delayed.subscribe(lambda _msgs: None)
    g.set("input", 2)
    delayed.down([(MessageType.TEARDOWN,)])
    time.sleep(0.05)
    assert g.get("delayed") == 1


def test_on_failure_stops_recovery_after_terminal_error() -> None:
    g = orchestration.pipeline("wf")
    src = state(0)
    g.add("src", src)

    def run(deps: list[object], _a: object) -> object:
        if int(deps[0]) >= 1:
            raise ValueError("boom")
        return deps[0]

    failing = orchestration.task(g, "failing", run, deps=["src"])
    recover_calls = [0]

    def recover(_err: object, _a: object) -> object:
        recover_calls[0] += 1
        raise ValueError("recover-failed")

    recovered = orchestration.on_failure(g, "recovered", failing, recover)
    recovered.subscribe(lambda _msgs: None)
    g.set("src", 1)
    g.set("src", 2)
    assert recover_calls[0] == 1


def test_loop_uses_permissive_parse_plus_truncate_for_iteration_dep_values() -> None:
    g = orchestration.pipeline("wf")
    source = state(2)
    iterations = state("2.9")
    g.add("input", source)
    g.add("iter", iterations)
    looped = orchestration.loop(
        g,
        "looped",
        "input",
        lambda value, _i, _a: int(value) * 2,
        iterations="iter",
    )
    looped.subscribe(lambda _msgs: None)
    assert g.get("looped") == 8
    g.set("iter", "bad")
    assert g.get("looped") == 4
    g.set("iter", "")
    assert g.get("looped") == 2


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


def test_gate_queues_values_and_approve_forwards_them() -> None:
    g = orchestration.pipeline("wf")
    src = state(0)
    g.add("input", src)
    ctrl = gate(g, "g", "input")
    assert isinstance(ctrl, GateController)
    forwarded: list[Any] = []

    def sink(msgs: object) -> None:
        for msg in msgs:  # type: ignore[union-attr]
            if msg[0] is MessageType.DATA:
                forwarded.append(msg[1])

    ctrl.node.subscribe(sink)
    ctrl.count.subscribe(lambda _: None)

    # Push values — they should be queued, not forwarded.
    g.set("input", 1)
    g.set("input", 2)
    g.set("input", 3)
    assert ctrl.pending.get() == [1, 2, 3]
    assert ctrl.count.get() == 3
    assert forwarded == []

    # Approve one at a time.
    ctrl.approve(1)
    assert forwarded == [1]
    assert ctrl.pending.get() == [2, 3]

    ctrl.approve(1)
    assert forwarded == [1, 2]
    assert ctrl.pending.get() == [3]


def test_gate_reject_discards_pending_values() -> None:
    g = orchestration.pipeline("wf")
    src = state(0)
    g.add("input", src)
    ctrl = gate(g, "g", "input")
    ctrl.node.subscribe(lambda _: None)

    g.set("input", "a")
    g.set("input", "b")
    assert ctrl.pending.get() == ["a", "b"]

    ctrl.reject(1)
    assert ctrl.pending.get() == ["b"]
    ctrl.reject(1)
    assert ctrl.pending.get() == []


def test_gate_modify_transforms_and_forwards() -> None:
    g = orchestration.pipeline("wf")
    src = state(0)
    g.add("input", src)
    ctrl = gate(g, "g", "input")
    forwarded: list[Any] = []

    def sink(msgs: object) -> None:
        for msg in msgs:  # type: ignore[union-attr]
            if msg[0] is MessageType.DATA:
                forwarded.append(msg[1])

    ctrl.node.subscribe(sink)

    g.set("input", 10)
    ctrl.modify(lambda val, _idx, _pending: val * 2)
    assert forwarded == [20]
    assert ctrl.pending.get() == []


def test_gate_open_flushes_pending_and_auto_approves_future() -> None:
    g = orchestration.pipeline("wf")
    src = state(0)
    g.add("input", src)
    ctrl = gate(g, "g", "input")
    forwarded: list[Any] = []

    def sink(msgs: object) -> None:
        for msg in msgs:  # type: ignore[union-attr]
            if msg[0] is MessageType.DATA:
                forwarded.append(msg[1])

    ctrl.node.subscribe(sink)

    g.set("input", 1)
    g.set("input", 2)
    assert forwarded == []

    ctrl.open()
    # Pending items flushed.
    assert forwarded == [1, 2]
    assert ctrl.is_open.get() is True

    # Future values auto-forward.
    g.set("input", 3)
    assert 3 in forwarded


def test_gate_close_re_enables_manual_gating() -> None:
    g = orchestration.pipeline("wf")
    src = state(0)
    g.add("input", src)
    ctrl = gate(g, "g", "input", start_open=True)
    forwarded: list[Any] = []

    def sink(msgs: object) -> None:
        for msg in msgs:  # type: ignore[union-attr]
            if msg[0] is MessageType.DATA:
                forwarded.append(msg[1])

    ctrl.node.subscribe(sink)

    g.set("input", 1)
    assert 1 in forwarded

    ctrl.close()
    assert ctrl.is_open.get() is False

    g.set("input", 2)
    # Value should be queued, not forwarded.
    assert ctrl.pending.get() == [2]
    assert 2 not in forwarded


def test_gate_max_pending_drops_when_full() -> None:
    g = orchestration.pipeline("wf")
    src = state(0)
    g.add("input", src)
    ctrl = gate(g, "g", "input", max_pending=2)
    ctrl.node.subscribe(lambda _: None)
    ctrl.count.subscribe(lambda _: None)

    g.set("input", 1)
    g.set("input", 2)
    g.set("input", 3)  # exceeds max_pending — FIFO-evicts oldest

    # Queue retains the two newest values; the oldest is evicted.
    assert ctrl.pending.get() == [2, 3]
    assert ctrl.count.get() == 2


def test_gate_approve_multiple() -> None:
    g = orchestration.pipeline("wf")
    src = state(0)
    g.add("input", src)
    ctrl = gate(g, "g", "input")
    forwarded: list[Any] = []

    def sink(msgs: object) -> None:
        for msg in msgs:  # type: ignore[union-attr]
            if msg[0] is MessageType.DATA:
                forwarded.append(msg[1])

    ctrl.node.subscribe(sink)

    g.set("input", "a")
    g.set("input", "b")
    g.set("input", "c")

    ctrl.approve(2)
    assert forwarded == ["a", "b"]
    assert ctrl.pending.get() == ["c"]


def test_gate_registers_internal_state_nodes() -> None:
    g = orchestration.pipeline("wf")
    src = state(0)
    g.add("input", src)
    gate(g, "g", "input")

    desc = g.describe()
    node_keys = set(desc["nodes"].keys())
    # The gate mounts internal state as a sub-graph.
    assert any("pending" in k for k in node_keys)
    assert any("is_open" in k for k in node_keys)
    assert any("count" in k for k in node_keys)


def test_gate_max_pending_below_one_raises() -> None:
    g = orchestration.pipeline("wf")
    src = state(0)
    g.add("input", src)
    with pytest.raises(ValueError, match="max_pending must be >= 1"):
        gate(g, "g", "input", max_pending=0)
