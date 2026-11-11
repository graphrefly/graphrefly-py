"""Phase 4: lifetimes, setup and cleanup, the abort signal, derived status.

Scenarios C-4, C-33 and C-34 (with its cleanup clause) of graphrefly
spec/conformance.jsonl, qualified against graphrefly._kernel (graphrefly-py:D2).
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import Harness, during

from graphrefly._kernel import COMPLETE, DATA, ERROR, Ctx, NodeStatus


def echo(ctx: Ctx) -> None:
    if ctx.deps[0].has:
        ctx.down([(DATA, ctx.deps[0].value)])


# ---- C-4: a sync leg and an async leg ------------------------------------------


def test_c4_the_async_result_is_a_later_wave_and_nothing_waits_across_it(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("A", 0)
    g.node("B", ["A"], echo)
    started: list[tuple[Ctx, object]] = []

    def c(ctx: Ctx) -> None:
        started.append((ctx, ctx.deps[0].value))  # starts work; sends nothing now

    g.node("C", ["A"], c)
    runs: list[tuple[object, object]] = []
    g.node("D", ["B", "C"], lambda ctx: runs.append((ctx.deps[0].value, ctx.deps[1].value)))
    g.subscribe("sc", "C")
    g.subscribe("sd", "D")
    runs.clear()

    assert during(g, lambda: g.set("A", 1)) == [
        "sc <- C: [PENDING, RESOLVED]",
        "sd <- D: [PENDING, RESOLVED]",
    ]
    assert runs == [(1, None)]  # B's new value, C's old (none yet); no PENDING left open
    assert g.status("D").cache != "stale" and g.status("C").cache != "stale"

    ctx, value = started[-1]
    assert during(g, lambda: ctx.down([(DATA, f"result {value}")])) == [
        'sc <- C: [PENDING, DATA("result 1")]',
        "sd <- D: [PENDING, RESOLVED]",
    ]
    assert runs[-1] == (1, "result 1")

    g.set("A", 2)
    late_ctx, _ = started[-1]
    for sink, node in (("sc", "C"), ("sd", "D")):
        g.unsubscribe(sink, node)
    assert late_ctx.signal.aborted
    assert during(g, lambda: late_ctx.down([(DATA, "too late")])) == []
    assert g.get("C").has is False


# ---- C-33: a closed lifetime revokes old ctx; cleanup failures stay isolated ----


def test_c33_lifetimes_setup_cleanup_status_and_revoked_ctx(harness: Harness) -> None:
    g = harness.graph
    log: list[str] = []
    g.state("A", 1)

    def p_setup(ctx: Ctx) -> Any:
        log.append("setup P")
        return lambda: log.append("cleanup P")

    g.node("P", ["A"], echo, setup=p_setup)
    kept: list[Ctx] = []
    statuses: list[NodeStatus] = []

    def n_setup(ctx: Ctx) -> Any:
        log.append("setup N")

        def cleanup() -> None:
            statuses.append(g.status("N"))
            log.append(f"cleanup N saw {ctx.state}")
            raise RuntimeError("cleanup failed")

        return cleanup

    prior: list[object] = []

    def n(ctx: Ctx) -> None:
        kept.append(ctx)
        prior.append(ctx.state)
        log.append(f"run N on {ctx.deps[0].value}")
        ctx.state = ctx.deps[0].value
        echo(ctx)

    g.node("N", ["P"], n, setup=n_setup)

    assert g.status("N") == NodeStatus("empty", "open", "inactive")
    g.subscribe("s", "N")
    assert g.status("N") == NodeStatus("ready", "open", "active")
    assert log == ["setup P", "setup N", "run N on 1"]

    g.set("A", 2)  # the second run runs no setup and drops no cleanup
    assert log[-1] == "run N on 2" and log.count("setup N") == 1
    g.invalidate("A")
    assert g.status("N").cache == "empty"
    g.set("A", 3)
    first_ctx = kept[-1]
    first_signal = first_ctx.signal
    assert not first_signal.aborted

    log.clear()
    g.unsubscribe("s", "N")
    # Dependencies are released first, so P's cleanup runs, then N's, which reads the
    # last run's value; its throw goes to the reporter and the close completes.
    assert log == ["cleanup P", "cleanup N saw 3"]
    assert statuses == [NodeStatus("ready", "open", "closing")]
    assert [(r.source, str(r.error)) for r in harness.reports] == [("cleanup N", "cleanup failed")]
    assert first_signal.aborted
    assert first_ctx.state is None  # the private state was dropped
    assert g.status("N") == NodeStatus("empty", "open", "inactive")
    assert during(g, lambda: first_ctx.down([(DATA, "late")])) == []

    log.clear()
    g.subscribe("s", "N")
    second_ctx = kept[-1]
    assert second_ctx is not first_ctx and second_ctx.signal is not first_signal
    assert not second_ctx.signal.aborted
    assert log == ["setup P", "setup N", "run N on 3"]  # fresh snapshots
    assert prior[-1] is None  # the new lifetime starts without the old private state
    first_ctx.state = "old"  # a write through the closed lifetime's ctx is dropped
    assert second_ctx.state == 3

    g.complete("A")
    assert g.status("N").output == "completed"
    assert log[-1] == "cleanup N saw 3"  # the ended node closed its lifetime at once


def test_c33_an_unsubscribe_requested_in_a_function_waits_for_the_wave(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 0)
    seen: list[str] = []

    def n(ctx: Ctx) -> None:
        if ctx.deps[0].value == 1:
            g.unsubscribe("s", "N")
            seen.append(g.status("N").lifecycle)
        echo(ctx)

    g.node("N", ["A"], n)
    g.subscribe("s", "N")
    assert during(g, lambda: g.set("A", 1)) == ["s <- N: [PENDING, DATA(1)]"]
    assert seen == ["active"]
    assert g.status("N").lifecycle == "inactive"


def test_c33_output_staged_by_a_lifetime_that_closes_before_commit_is_dropped(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("A", 0)
    g.state("T", 0)
    kept: list[Ctx] = []
    g.node("N", ["A"], lambda ctx: kept.append(ctx))
    g.subscribe("n", "N")
    old = kept[-1]

    def t_sink(array: tuple[Any, ...]) -> None:
        if array == (("PENDING",), (DATA, 1)):
            g.unsubscribe("n", "N")  # queued: closes N's lifetime first
            g.batch(lambda: old.down([(DATA, "staged")]))  # commit queued behind it

    g.subscribe("t", "T", t_sink)
    g.set("T", 1)
    assert g.get("N").values == ()
    g.subscribe("n2", "N")
    assert g.get("N").values == ()  # not retained by the new lifetime either


# ---- C-34: the cleanup clause --------------------------------------------------


def test_c34_an_ended_nodes_cleanup_runs_after_the_fanout(harness: Harness) -> None:
    g = harness.graph
    g.state("T", 0)
    log: list[str] = []

    def s(ctx: Ctx) -> None:
        if ctx.deps[0].value:
            ctx.down([(DATA, 1), (COMPLETE,)])

    g.node("S", ["T"], s, setup=lambda ctx: lambda: log.append("cleanup S"))

    def x(array: tuple[Any, ...]) -> None:
        log.append(f"X {len(array)}")

    g.subscribe("X", "S", x)
    g.subscribe("Y", "S")
    log.clear()
    g.set("T", 1)
    assert log == ["X 3", "cleanup S"]
    assert g.get("T").active is False
    assert g.status("S") == NodeStatus("ready", "completed", "active")


# ---- Setup and signal --------------------------------------------------------


def test_setup_output_is_part_of_the_first_run_and_its_throw_ends_the_node(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("A", 1)

    def setup(ctx: Ctx) -> None:
        ctx.down([(DATA, "from setup")])

    g.node("N", ["A"], lambda ctx: None, setup=setup)
    assert during(g, lambda: g.subscribe("s", "N")) == ['s <- N: [START, DATA("from setup")]']
    # Setup and function output are separate arrays of one run; the snapshot is the last.
    g.node("N2", ["A"], echo, setup=setup)
    assert during(g, lambda: g.subscribe("s2", "N2")) == ["s2 <- N2: [START, DATA(1)]"]

    boom = ValueError("setup failed")

    def failing(ctx: Ctx) -> None:
        raise boom

    ran: list[int] = []
    g.node("M", ["A"], lambda ctx: ran.append(1), setup=failing)
    g.subscribe("m", "M")
    assert g.get("M").ended == (ERROR, boom) and ran == []


def test_the_signal_is_aborted_at_close_not_by_invalidate_or_a_rerun(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 1)
    kept: list[Ctx] = []
    events: list[str] = []

    def n(ctx: Ctx) -> None:
        kept.append(ctx)
        echo(ctx)

    g.node("N", ["A"], n)
    g.subscribe("s", "N")
    signal = kept[-1].signal
    remove = signal.on_abort(lambda: events.append("removed"))
    remove()
    signal.on_abort(lambda: events.append("aborted"))
    g.invalidate("A")
    g.set("A", 2)
    assert kept[-1].signal is signal and not signal.aborted
    g.unsubscribe("s", "N")
    assert signal.aborted and events == ["aborted"]
    signal.on_abort(lambda: events.append("late listener"))
    assert events == ["aborted", "late listener"]


def test_a_signal_listener_failure_is_reported_and_a_fatal_one_propagates(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("A", 1)
    kept: list[Ctx] = []
    g.node("N", ["A"], lambda ctx: kept.append(ctx))
    g.subscribe("s", "N")

    def failing() -> None:
        raise RuntimeError("listener failed")

    kept[-1].signal.on_abort(failing)
    g.unsubscribe("s", "N")
    assert [(r.source, str(r.error)) for r in harness.reports] == [("signal N", "listener failed")]

    g.subscribe("s", "N")

    def fatal() -> None:
        raise KeyboardInterrupt

    kept[-1].signal.on_abort(fatal)
    with pytest.raises(KeyboardInterrupt):
        g.unsubscribe("s", "N")


def test_the_setup_runs_again_on_every_activation(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 1)
    setups: list[int] = []
    g.node("N", ["A"], echo, setup=lambda ctx: setups.append(1))
    for _ in range(3):
        g.subscribe("s", "N")
        g.set("A", 2)
        g.unsubscribe("s", "N")
    assert setups == [1, 1, 1]
