"""Phase 3: COMPLETE, ERROR and INVALIDATE, their auto and manual policies, ended nodes.

Scenarios C-3, C-15, C-17, C-36, C-40 except its replace clause, the terminal and
manual-INVALIDATE clauses of C-44, and C-34 except its cleanup clause, of graphrefly
spec/conformance.jsonl, qualified against graphrefly._kernel (graphrefly-py:D2).
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import Harness, Producer, during

from graphrefly._kernel import COMPLETE, DATA, ERROR, INVALIDATE, Ctx, KernelError


def echo(ctx: Ctx) -> None:
    # Sends the values that arrived: a run may bring none, as the run that sees a last
    # terminal alone (R-invocation, R-terminals).
    for wave in ctx.deps[0].waves:
        if wave:
            ctx.down([(DATA, wave[-1])])


def terminal_kinds(ctx: Ctx) -> list[str]:
    return [d.terminal[0] if d.terminal else "-" for d in ctx.deps]


# ---- C-3: INVALIDATE through a diamond ----------------------------------------


def _diamond_with_count(harness: Harness, policy: str) -> tuple[list[str], Any]:
    g = harness.graph
    g.state("S", 1)
    g.node("B", ["S"], echo)
    g.node("C", ["S"], echo)
    runs: list[str] = []

    def d(ctx: Ctx) -> None:
        if any(dep.invalidated for dep in ctx.deps):
            runs.append("invalidated:" + ",".join(str(dep.invalidated) for dep in ctx.deps))
            ctx.down([(INVALIDATE,)])  # the function decides; it sends the one INVALIDATE
            return
        count = (ctx.state if isinstance(ctx.state, int) else 0) + 1
        ctx.state = count
        runs.append(f"run:{count}")
        ctx.down([(DATA, count)])

    g.node("D", ["B", "C"], d, invalidate=policy)  # type: ignore[arg-type]
    g.subscribe("sd", "D")
    g.set("S", 2)
    return runs, d


def test_c3_auto_invalidate_reaches_d_once_and_keeps_private_state(harness: Harness) -> None:
    g = harness.graph
    runs, _ = _diamond_with_count(harness, "auto")
    assert runs == ["run:1", "run:2"] and g.get("D").values == (2,)

    assert during(g, lambda: g.invalidate("S")) == ["sd <- D: [PENDING, INVALIDATE]"]
    assert runs == ["run:1", "run:2"]  # D's function did not run for it
    assert g.get("D").has is False

    # S has no value now, so a second INVALIDATE is dropped and starts no wave.
    assert during(g, lambda: g.invalidate("S")) == []

    assert during(g, lambda: g.set("S", 3)) == ["sd <- D: [PENDING, DATA(3)]"]
    assert runs[-1] == "run:3"  # the count continued: private state was kept


def test_c3_manual_invalidate_runs_d_once_with_both_inputs_invalidated(
    harness: Harness,
) -> None:
    g = harness.graph
    runs, _ = _diamond_with_count(harness, "manual")
    assert during(g, lambda: g.invalidate("S")) == ["sd <- D: [PENDING, INVALIDATE]"]
    assert runs[-1] == "invalidated:True,True"
    assert len([r for r in runs if r.startswith("invalidated")]) == 1
    assert during(g, lambda: g.set("S", 3)) == ["sd <- D: [PENDING, DATA(3)]"]


def test_c3_manual_node_keeps_its_value_until_its_function_clears_it(harness: Harness) -> None:
    g = harness.graph
    g.state("S", 1)
    g.node("D", ["S"], echo, invalidate="manual")
    g.subscribe("sd", "D")
    assert during(g, lambda: g.invalidate("S")) == ["sd <- D: [PENDING, RESOLVED]"]
    assert g.get("D").values == (1,)


# ---- C-15: a dependency's terminal settles its pending mark --------------------


def _c15(harness: Harness) -> tuple[list[tuple[list[str], list[list[list[object]]]]], Any]:
    g = harness.graph
    g.state("A")  # no value, so B completes without one
    g.state("A2", 0)
    g.node("B", ["A"], echo)
    g.node("C", ["A2"], echo)
    runs: list[tuple[list[str], list[list[list[object]]]]] = []

    def d(ctx: Ctx) -> None:
        runs.append((terminal_kinds(ctx), [dep.waves for dep in ctx.deps]))

    g.node("D", ["B", "C"], d, complete="manual", error="manual")
    g.node("E", ["D"], lambda ctx: None)
    g.subscribe("sd", "D")
    g.subscribe("se", "E")
    runs.clear()
    return runs, g


def test_c15a_a_completion_settles_like_a_value(harness: Harness) -> None:
    runs, g = _c15(harness)

    def body() -> None:
        g.complete("A")
        g.set("A2", 5)

    g.batch(body)
    assert runs == [(["COMPLETE", "-"], [[], [[5]]])]


def test_c15b_a_terminal_alone_runs_d_once_and_nothing_is_left_pending(
    harness: Harness,
) -> None:
    runs, g = _c15(harness)
    assert during(g, lambda: g.complete("A")) == [
        "sd <- D: [PENDING, RESOLVED]",
        "se <- E: [PENDING, RESOLVED]",
    ]
    assert runs == [(["COMPLETE", "-"], [[], []])]


def test_c15c_an_error_and_a_value_reach_d_in_one_run(harness: Harness) -> None:
    runs, g = _c15(harness)

    def body() -> None:
        g.error("A", "boom")
        g.set("A2", 6)

    g.batch(body)
    assert runs == [(["ERROR", "-"], [[], [[6]]])]
    assert g.get("B").ended == (ERROR, "boom")


# ---- C-17: an ended dependency counts toward auto COMPLETE ----------------------


@pytest.mark.parametrize("order", ["error-first", "complete-first"])
def test_c17_auto_complete_after_every_dependency_ended_in_any_order(
    harness: Harness, order: str
) -> None:
    g = harness.graph
    g.state("B", 0)
    g.state("C", 0)
    runs: list[list[str]] = []
    dctrl_runs: list[int] = []

    def d(ctx: Ctx) -> None:
        runs.append(terminal_kinds(ctx))

    def dctrl(ctx: Ctx) -> None:
        dctrl_runs.append(1)

    g.node("D", ["B", "C"], d, error="manual")
    g.node("Dctrl", ["B", "C"], dctrl)
    sd = harness.sink("D")
    sctrl = harness.sink("Dctrl")
    runs.clear()
    dctrl_runs.clear()

    def b_value_and_complete() -> None:
        g.set("B", 1)
        g.complete("B")

    if order == "error-first":
        g.error("C", "e")
        assert dctrl_runs == []  # Dctrl ended with C's ERROR without running
        assert runs == [["-", "ERROR"]] and g.get("D").ended is None
        assert sctrl.arrays[-1] == (("PENDING",), (ERROR, "e"))
        g.batch(b_value_and_complete)
        assert runs[-1] == ["COMPLETE", "-"]
    else:
        g.batch(b_value_and_complete)
        assert runs == [["COMPLETE", "-"]] and g.get("D").ended is None
        before = len(dctrl_runs)  # Dctrl ran on B's value
        g.error("C", "e")
        assert len(dctrl_runs) == before  # and ended with C's ERROR without running
        assert runs[-1] == ["-", "ERROR"]
        assert sctrl.arrays[-1] == (("PENDING",), (ERROR, "e"))
    assert g.get("D").ended == (COMPLETE,)
    assert sum(1 for a in sd.arrays if a[-1] == (COMPLETE,)) == 1
    assert g.get("Dctrl").ended == (ERROR, "e")


def test_c17_the_run_that_sees_the_last_terminal_alone_runs_once_then_completes(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("S", 1)
    runs: list[list[str]] = []
    g.node("N", ["S"], lambda ctx: runs.append(terminal_kinds(ctx)))
    sink = harness.sink("N")
    assert runs == [["-"]]
    g.complete("S")  # the last terminal arrives alone: the function still sees it
    assert runs == [["-"], ["COMPLETE"]]
    assert sink.arrays[-1] == (("PENDING",), (COMPLETE,))
    assert g.get("N").ended == (COMPLETE,)


# ---- C-34: terminal fanout and the ended node's lifetime closing --------------


def test_c34_terminal_fanout_closes_the_lifetime_and_an_ended_node_gives_its_snapshot(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("T", 0)
    runs: list[int] = []

    def s(ctx: Ctx) -> None:
        runs.append(1)
        if ctx.deps[0].value:
            ctx.down([(DATA, 1), (COMPLETE,)])

    g.node("S", ["T"], s)
    subscribed = [False]

    def x(array: tuple[Any, ...]) -> None:
        if array[0] == ("PENDING",) and not subscribed[0]:
            subscribed[0] = True
            g.subscribe("Z", "S")
            raise RuntimeError("x failed")

    g.subscribe("X", "S", x)
    g.subscribe("Y", "S")
    runs.clear()
    assert during(g, lambda: g.set("T", 1)) == [
        "X <- S: [PENDING, DATA(1), COMPLETE]",
        "Y <- S: [PENDING, DATA(1), COMPLETE]",
        "Z <- S: [START, DATA(1), COMPLETE]",
    ]
    assert runs == [1]
    assert g.get("T").active is False  # S released T, which had no other dependent
    state = g.get("S")
    assert state.active and state.values == (1,) and state.ended == (COMPLETE,)
    assert [(r.source, str(r.error)) for r in harness.reports] == [("sink X", "x failed")]
    g.set("T", 2)
    assert runs == [1]  # S never runs again

    # Deactivated and subscribed again, the ended node gives its snapshot, runs
    # nothing and acquires nothing upstream.
    for sink in ("X", "Y", "Z"):
        g.unsubscribe(sink, "S")
    assert g.get("S").active is False
    assert during(g, lambda: g.subscribe("W", "S")) == ["W <- S: [START, DATA(1), COMPLETE]"]
    assert runs == [1] and g.get("T").active is False


# ---- C-36: input arrays and terminal admission in one invocation ---------------


def test_c36_manual_complete_sees_value_and_terminal_in_one_invocation(harness: Harness) -> None:
    g = harness.graph
    a = Producer()
    g.node("A", [], a)
    seen: list[tuple[list[list[object]], Any]] = []

    def d(ctx: Ctx) -> None:
        seen.append((ctx.deps[0].waves, ctx.deps[0].terminal))

    g.node("d", ["A"], d, complete="manual")
    g.subscribe("s", "d")
    seen.clear()
    a.down([(DATA, 20), (COMPLETE,)])
    assert seen == [([[20]], (COMPLETE,))]
    assert g.get("d").ended is None  # open until its function ends it


def test_c36_auto_complete_follows_the_run_without_a_balancing_resolved(
    harness: Harness,
) -> None:
    g = harness.graph
    a = Producer()
    g.node("A", [], a)
    g.node("d", ["A"], echo)
    g.subscribe("s", "d")
    assert during(g, lambda: a.down([(DATA, 5), (COMPLETE,)])) == [
        "s <- d: [PENDING, DATA(5)]",
        "s <- d: [PENDING, COMPLETE]",
    ]


def test_c36_data_then_invalidate_is_published_as_invalidate(harness: Harness) -> None:
    g = harness.graph
    a = Producer()
    g.node("A", [], a)
    runs: list[int] = []

    def d(ctx: Ctx) -> None:
        runs.append(1)
        echo(ctx)

    g.node("d", ["A"], d)
    g.subscribe("sa", "A")
    g.subscribe("sd", "d")
    a.down([(DATA, 1)])
    runs.clear()
    assert during(g, lambda: a.down([(DATA, 7), (INVALIDATE,)])) == [
        "sa <- A: [PENDING, INVALIDATE]",
        "sd <- d: [PENDING, INVALIDATE]",
    ]
    assert runs == []
    assert g.get("A").has is False and g.get("d").has is False


# ---- C-40: an ended node can be subscribed and depended on ---------------------


def test_c40_an_ended_node_can_be_subscribed_and_depended_on(harness: Harness) -> None:
    g = harness.graph
    g.state("S", 0)
    g.state("T", 0)
    g.set("S", 1)
    g.complete("S")
    g.error("T", "e")
    assert during(g, lambda: g.subscribe("k", "S")) == ["k <- S: [START, DATA(1), COMPLETE]"]

    d_runs: list[list[list[object]]] = []
    e_runs: list[int] = []
    m_runs: list[list[str]] = []
    g.node("D", ["S"], lambda ctx: d_runs.append(ctx.deps[0].waves))
    g.node("E", ["T"], lambda ctx: e_runs.append(1))
    g.node(
        "M",
        ["T"],
        lambda ctx: m_runs.append(terminal_kinds(ctx)),
        error="manual",
        complete="manual",
    )
    assert during(g, lambda: g.subscribe("d", "D")) == ["d <- D: [START, COMPLETE]"]
    assert d_runs == [[[1]]]
    assert during(g, lambda: g.subscribe("e", "E")) == ['e <- E: [START, ERROR("e")]']
    assert e_runs == []
    g.subscribe("m", "M")
    assert m_runs == [["ERROR"]] and g.get("M").ended is None

    with pytest.raises(KernelError, match="has ended"):
        g.set("S", 2)
    assert g.get("S").values == (1,)


# ---- C-44: a wave of terminals or INVALIDATEs holds a fan-in -----------------


@pytest.mark.parametrize("end", ["complete", "error"])
def test_c44_a_wave_of_terminals_holds_a_manual_fan_in_until_both_legs_arrive(
    harness: Harness, end: str
) -> None:
    g = harness.graph
    g.state("S", 1)
    g.node("B", ["S"], echo)
    g.node("C", ["S"], echo)
    runs: list[list[str]] = []
    policy: dict[str, Any] = {"complete": "manual"} if end == "complete" else {"error": "manual"}
    g.node("D", ["B", "C"], lambda ctx: runs.append(terminal_kinds(ctx)), **policy)
    g.subscribe("sb", "B")
    g.subscribe("s", "D")
    runs.clear()
    lines = during(g, lambda: g.complete("S") if end == "complete" else g.error("S", "e"))
    kind = "COMPLETE" if end == "complete" else 'ERROR("e")'
    assert lines[0] == f"sb <- B: [PENDING, {kind}]"
    assert runs == ([["COMPLETE", "COMPLETE"]] if end == "complete" else [["ERROR", "ERROR"]])


def test_c44_a_manual_invalidate_fan_in_runs_once_seeing_both_legs(harness: Harness) -> None:
    g = harness.graph
    g.state("S", 1)
    g.node("B", ["S"], echo)
    g.node("C", ["S"], echo)
    runs: list[str] = []

    def d(ctx: Ctx) -> None:
        runs.append(",".join("inv" if dep.invalidated else "-" for dep in ctx.deps))

    g.node("D", ["B", "C"], d, invalidate="manual")
    g.subscribe("s", "D")
    runs.clear()
    assert during(g, lambda: g.invalidate("S")) == ["s <- D: [PENDING, RESOLVED]"]
    assert runs == ["inv,inv"]


def test_an_auto_complete_goes_out_with_the_run_so_a_manual_dependent_runs_once(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("a", 1)
    g.node("d", ["a"], echo)
    runs: list[str] = []
    g.node(
        "e",
        ["d"],
        lambda ctx: runs.append(f"{ctx.deps[0].waves} {terminal_kinds(ctx)[0]}"),
        complete="manual",
    )
    g.subscribe("s", "e")
    g.subscribe("sd", "d")
    runs.clear()

    def body() -> None:
        g.set("a", 2)
        g.complete("a")

    assert during(g, lambda: g.batch(body)) == [
        "s <- e: [PENDING, RESOLVED]",
        "sd <- d: [PENDING, DATA(2)]",
        "sd <- d: [PENDING, COMPLETE]",
    ]
    assert runs == ["[[2]] COMPLETE"]


@pytest.mark.parametrize("policy", ["auto", "manual"])
def test_an_invalidate_before_an_auto_error_takes_effect_first(
    harness: Harness, policy: str
) -> None:
    """R-terminals: under auto ERROR, an earlier INVALIDATE in the wave applies first."""

    g = harness.graph
    g.state("a", 1)
    g.node("m", ["a"], echo, invalidate=policy)  # type: ignore[arg-type]
    g.subscribe("s", "m")

    def body() -> None:
        g.invalidate("a")
        g.error("a", "boom")

    assert during(g, lambda: g.batch(body)) == [
        "s <- m: [PENDING, INVALIDATE]",
        's <- m: [PENDING, ERROR("boom")]',
    ]
    assert during(g, lambda: g.subscribe("s2", "m")) == ['s2 <- m: [START, ERROR("boom")]']


def test_host_input_to_an_ended_state_is_refused_also_when_queued(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 0)
    g.state("B", 0)

    def sink(array: tuple[Any, ...]) -> None:
        if array == (("PENDING",), (DATA, 1)):
            g.complete("A")  # queued: runs after this wave
            g.set("A", 9)  # queued behind it: finds A ended and is refused

    g.subscribe("b", "B", sink)
    g.subscribe("a", "A")
    g.set("B", 1)
    assert g.get("A").values == (0,) and g.get("A").ended == (COMPLETE,)
    assert [r.source for r in harness.reports] == ["queued request"]
    assert "has ended" in str(harness.reports[0].error)


def test_a_queued_batch_commit_to_a_state_that_ended_first_is_refused_whole(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("S", 1)
    g.state("U", 1)
    g.state("T", 0)
    g.node("N", ["S"], echo)
    ran: list[object] = []

    def ends_s(array: tuple[Any, ...]) -> None:
        if (DATA, 1) in array:
            g.complete("S")

    def batches(array: tuple[Any, ...]) -> None:
        if (DATA, 1) in array:

            def body() -> None:
                g.set("U", 2)

                def updater(value: object) -> object:
                    ran.append(value)
                    return 5

                g.set("S", updater)
                g.replace("N", ["U"], echo)

            g.batch(body)

    g.subscribe("first", "T", ends_s)
    g.subscribe("second", "T", batches)
    g.set("T", 1)
    assert ran == []  # the updater never ran on the ended state
    assert g.get("S").ended == (COMPLETE,) and g.get("U").value == 1  # nothing committed
    assert [(r.source, str(r.error)) for r in harness.reports] == [
        ("queued request", "S has ended (batch)")
    ]
    assert g.definition("N").deps == ("U",)  # the body's topology request still ran
