"""Phase 2: waves, PENDING prefixes, the kernel's RESOLVED, snapshots, quiet nodes, batch.

Scenarios C-12, C-16, C-23, C-28, C-41 and the value and quiet-node clauses of C-44
of graphrefly spec/conformance.jsonl, qualified against graphrefly._kernel
(graphrefly-py:D2).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from conftest import Harness, Producer, during

from graphrefly._kernel import (
    COMPLETE,
    DATA,
    ERROR,
    INVALIDATE,
    Ctx,
    Graph,
    KernelError,
    Report,
)


def map_fn(f: Any) -> Any:
    """One output array per input array, as the reference sugar's map."""

    def run(ctx: Ctx) -> None:
        for wave in ctx.deps[0].waves:
            if wave:
                ctx.down([(DATA, f(v)) for v in wave])

    return run


def echo(ctx: Ctx) -> None:
    ctx.down([(DATA, ctx.deps[0].value)])


# ---- C-12: occurrences stay DATA; RESOLVED only settles a node that sent nothing ----


def test_c12a_equal_values_are_distinct_occurrences(harness: Harness) -> None:
    g = harness.graph
    source = Producer()
    g.node("S", [], source)
    g.state("T", 1)
    assert during(g, lambda: g.subscribe("k", "S")) == ["k <- S: [START]"]
    assert during(g, lambda: source.down([(DATA, 1), (DATA, 1), (DATA, 1), (COMPLETE,)])) == [
        "k <- S: [PENDING, DATA(1), DATA(1), DATA(1), COMPLETE]"
    ]
    g.subscribe("t", "T")

    def three_writes() -> None:
        g.set("T", 1)
        g.set("T", 1)
        g.set("T", 1)
        g.complete("T")

    assert during(g, three_writes) == [
        "t <- T: [PENDING, DATA(1)]",
        "t <- T: [PENDING, DATA(1)]",
        "t <- T: [PENDING, DATA(1)]",
        "t <- T: [PENDING, COMPLETE]",
    ]


def test_c12b_a_silent_pending_node_gets_exactly_one_kernel_resolved(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 0)
    g.node("N", ["A"], lambda ctx: None)
    runs: list[list[list[object]]] = []

    def e(ctx: Ctx) -> None:
        runs.append(ctx.deps[0].waves)

    g.node("E", ["N"], e)
    g.subscribe("n", "N")
    g.subscribe("e", "E")
    runs.clear()
    for value in (1, 2, 3):
        assert during(g, lambda v=value: g.set("A", v)) == [  # type: ignore[misc]
            "n <- N: [PENDING, RESOLVED]",
            "e <- E: [PENDING, RESOLVED]",
        ]
    # E ran on each RESOLVED as an empty array, sent nothing, and was settled in turn.
    assert runs == [[[]], [[]], [[]]]
    assert g.get("N").has is False


# ---- C-16: a quiet node absorbs its input waves -----------------------------


def test_c16_a_quiet_node_marks_nothing_pending_and_its_output_is_a_later_wave(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("A", 0)
    kept: list[Ctx] = []

    def q(ctx: Ctx) -> None:
        kept.append(ctx)
        held = ctx.state if isinstance(ctx.state, list) else []
        ctx.state = [*held, *(v for wave in ctx.deps[0].waves for v in wave)]

    g.node("Q", ["A"], q, quiet=True)
    e_runs: list[list[list[object]]] = []

    def e(ctx: Ctx) -> None:
        e_runs.append(ctx.deps[0].waves)
        ctx.down([(DATA, len(ctx.deps[0].waves))])

    g.node("E", ["Q"], e)
    g.subscribe("q", "Q")
    g.subscribe("e", "E")
    e_runs.clear()

    def writes() -> None:
        for v in (1, 2, 3):
            g.set("A", v)

    assert during(g, writes) == []  # not even PENDING reaches Q's sink or E
    assert e_runs == []
    assert g.get("E").values == (0,)  # still what E's first run sent
    ctx = kept[-1]
    held = ctx.state
    assert held == [0, 1, 2, 3]
    assert during(g, lambda: ctx.down([(DATA, v) for v in [1, 2, 3]])) == [
        "q <- Q: [PENDING, DATA(1), DATA(2), DATA(3)]",
        "e <- E: [PENDING, DATA(1)]",
    ]
    assert e_runs == [[[1, 2, 3]]]
    assert during(g, lambda: g.subscribe("late", "Q")) == [
        "late <- Q: [START, DATA(1), DATA(2), DATA(3)]"
    ]


def test_c16_a_quiet_node_forwards_an_auto_invalidate_at_once(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 1)
    kept: list[Ctx] = []
    g.node("Q", ["A"], lambda ctx: kept.append(ctx), quiet=True)
    g.subscribe("q", "Q")
    kept[-1].down([(DATA, "held")])
    assert during(g, lambda: g.invalidate("A")) == ["q <- Q: [PENDING, INVALIDATE]"]


@pytest.mark.parametrize("policy", ["auto", "manual"])
def test_c16_a_quiet_nodes_invalidate_marks_its_cone_so_a_diamond_settles_once(
    harness: Harness, policy: str
) -> None:
    g = harness.graph
    g.state("A", 1)

    def passes(ctx: Ctx) -> None:
        for wave in ctx.deps[0].waves:
            if wave:
                ctx.down([(DATA, wave[-1])])

    g.node("Q", ["A"], passes, quiet=True)
    g.node("B", ["Q"], passes)
    g.node("C", ["Q"], passes)
    runs: list[list[bool]] = []
    g.node(
        "D",
        ["B", "C"],
        lambda ctx: runs.append([dep.invalidated for dep in ctx.deps]),
        invalidate=policy,  # type: ignore[arg-type]
    )
    g.subscribe("d", "D")
    assert g.get("B").has and g.get("C").has
    runs.clear()
    lines = during(g, lambda: g.invalidate("A"))
    if policy == "auto":
        assert lines == ["d <- D: [PENDING, RESOLVED]"] and runs == []  # settled once
    else:
        assert runs == [[True, True]]  # one run, both legs invalidated


# ---- C-23: ctx.deps is the function's only input ----------------------------


def test_c23_ctx_deps_shows_arrays_terminal_and_latest_value(harness: Harness) -> None:
    g = harness.graph
    a = Producer()
    g.node("A", [], a)
    g.state("B", "b0")
    seen: list[list[tuple[Any, ...]]] = []

    def n(ctx: Ctx) -> None:
        seen.append([(d.id, d.waves, d.terminal, d.has, d.value) for d in ctx.deps])

    def first(index: int = 0) -> tuple[Any, ...]:
        return seen[-1][index]

    g.node("N", ["A", "B"], n, complete="manual", error="manual")
    g.subscribe("n", "N")
    assert seen[-1] == [("A", [], None, False, None), ("B", [["b0"]], None, True, "b0")]

    # (a) Write B only: A's entry has no arrays.
    g.set("B", "b1")
    assert seen[-1][0] == ("A", [], None, False, None)
    assert seen[-1][1][1] == [["b1"]]

    # (b) A sends RESOLVED alone: A's entry has one empty array. A is a node that,
    # marked pending, sent nothing, so the kernel settled it with RESOLVED.
    g.state("T", 0)
    g.node("R", ["T"], lambda ctx: None)
    seen_r: list[list[list[object]]] = []
    g.node("NR", ["R"], lambda ctx: seen_r.append(ctx.deps[0].waves), complete="manual")
    g.subscribe("nr", "NR")
    seen_r.clear()
    g.set("T", 1)
    assert seen_r == [[[]]]

    # (c) [DATA 1, DATA 2, INVALIDATE, DATA 3] is split at the INVALIDATE: N sees [3].
    seen.clear()
    a.down([(DATA, 1), (DATA, 2), (INVALIDATE,), (DATA, 3)])
    assert first() == ("A", [[3]], None, True, 3)

    # (d) DATA None and DATA [] are ordinary payloads.
    a.down([(DATA, None)])
    assert first() == ("A", [[None]], None, True, None)
    a.down([(DATA, [])])
    assert first() == ("A", [[[]]], None, True, [])

    # (e) The terminals are A's COMPLETE and B's ERROR e; never among the arrays.
    def ends() -> None:
        a.down([(COMPLETE,)])
        g.error("B", "e")

    g.batch(ends)
    (a_entry, b_entry) = seen[-1]
    assert a_entry[1] == [] and a_entry[2] == (COMPLETE,)
    assert b_entry[1] == [] and b_entry[2] == (ERROR, "e")


def test_c23_what_came_before_an_inputs_last_invalidate_is_gone(harness: Harness) -> None:
    """C-23 (c) across arrays: a run sending [1], [INVALIDATE], [2] leaves its dependent [[2]]."""

    g = harness.graph
    g.state("A", 0)

    def b(ctx: Ctx) -> None:
        if ctx.deps[0].value:
            ctx.down([(DATA, 1)])
            ctx.down([(INVALIDATE,)])
            ctx.down([(DATA, 2)])
        else:
            ctx.down([(DATA, 0)])

    g.node("B", ["A"], b)
    seen: list[list[list[object]]] = []
    g.node("N", ["B"], lambda ctx: seen.append(ctx.deps[0].waves))
    g.subscribe("n", "N")
    seen.clear()
    g.set("A", 1)
    assert seen == [[[2]]]


def test_c23_invalidated_is_shown_only_under_the_manual_policy(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 1)
    flags: dict[str, list[bool]] = {"auto": [], "manual": []}

    def watcher(policy: str) -> Any:
        def run(ctx: Ctx) -> None:
            flags[policy].append(ctx.deps[0].invalidated)
            if ctx.deps[0].has:
                ctx.down([(DATA, ctx.deps[0].value)])

        return run

    g.node("auto", ["A"], watcher("auto"))
    g.node("manual", ["A"], watcher("manual"), invalidate="manual")
    g.subscribe("x", "auto")
    g.subscribe("y", "manual")

    def invalidate_then_write() -> None:
        g.invalidate("A")
        g.set("A", 2)

    g.batch(invalidate_then_write)
    assert flags == {"auto": [False, False], "manual": [False, True]}


# ---- C-28: intact output arrays, kernel-only PENDING prefixes, written order ----


def test_c28_one_array_of_three_occurrences_stays_intact_through_a_diamond(
    harness: Harness,
) -> None:
    g = harness.graph
    a = Producer()
    g.node("A", [], a)
    g.node("B", ["A"], map_fn(lambda v: v))
    g.node("C", ["A"], map_fn(lambda v: v))
    seen: list[list[list[list[object]]]] = []
    g.node("D", ["B", "C"], lambda ctx: seen.append([d.waves for d in ctx.deps]))
    g.subscribe("o1", "A")
    g.subscribe("o2", "A")
    g.subscribe("sd", "D")
    seen.clear()
    assert during(g, lambda: a.down([(DATA, 23), (DATA, 23), (DATA, 30)])) == [
        "o1 <- A: [PENDING, DATA(23), DATA(23), DATA(30)]",
        "o2 <- A: [PENDING, DATA(23), DATA(23), DATA(30)]",
        "sd <- D: [PENDING, RESOLVED]",
    ]
    assert seen == [[[[23, 23, 30]], [[23, 23, 30]]]]


def test_c28_separate_calls_stay_separate_arrays_and_a_batch_accumulates(
    harness: Harness,
) -> None:
    g = harness.graph
    a = Producer()
    g.node("A", [], a)
    g.state("S", 0)
    g.subscribe("o", "A")
    g.subscribe("s", "S")

    def two_calls() -> None:
        a.down([(DATA, 23)])
        a.down([(DATA, 30)])

    assert during(g, two_calls) == ["o <- A: [PENDING, DATA(23)]", "o <- A: [PENDING, DATA(30)]"]
    assert during(g, lambda: g.batch(two_calls)) == ["o <- A: [PENDING, DATA(23), DATA(30)]"]

    def three_sets() -> None:
        for v in (1, 2, 3):
            g.set("S", v)

    assert during(g, three_sets) == [
        "s <- S: [PENDING, DATA(1)]",
        "s <- S: [PENDING, DATA(2)]",
        "s <- S: [PENDING, DATA(3)]",
    ]
    assert during(g, lambda: g.batch(three_sets)) == [
        "s <- S: [PENDING, DATA(1), DATA(2), DATA(3)]"
    ]


def test_c28_sink_requests_run_after_the_whole_fanout(harness: Harness) -> None:
    g = harness.graph
    a = Producer()
    g.node("A", [], a)
    first = [True]

    def o1(array: tuple[Any, ...]) -> None:
        if not first[0] or array[0][0] != "PENDING":
            return
        first[0] = False
        g.unsubscribe("o2", "A")  # would cut o2 off if applied now
        g.subscribe("o3", "A")
        a.down([(DATA, 99)])

    g.subscribe("o1", "A", o1)
    g.subscribe("o2", "A")
    assert during(g, lambda: a.down([(DATA, 1)])) == [
        "o1 <- A: [PENDING, DATA(1)]",
        "o2 <- A: [PENDING, DATA(1)]",
        "o3 <- A: [START, DATA(1)]",
        "o1 <- A: [PENDING, DATA(99)]",
        "o3 <- A: [PENDING, DATA(99)]",
    ]


def test_c28_an_array_is_split_at_its_last_invalidate(harness: Harness) -> None:
    g = harness.graph
    a = Producer()
    g.node("A", [], a)
    g.subscribe("o", "A")
    a.down([(DATA, 0)])
    assert during(g, lambda: a.down([(DATA, 1), (INVALIDATE,), (DATA, 2), (DATA, 3)])) == [
        "o <- A: [PENDING, INVALIDATE]",
        "o <- A: [PENDING, DATA(2), DATA(3)]",
    ]


@pytest.mark.parametrize("array", [[(DATA, 2), ("RESOLVED",)], [("RESOLVED",)], [("PENDING",)]])
def test_c28_pending_and_resolved_in_output_end_the_node(harness: Harness, array: Any) -> None:
    g = harness.graph
    g.state("A", 0)

    def n(ctx: Ctx) -> None:
        if ctx.deps[0].value:
            ctx.down(array)

    g.node("N", ["A"], n)
    g.subscribe("o", "N")
    lines = during(g, lambda: g.set("A", 1))
    assert len(lines) == 1 and lines[0].startswith("o <- N: [PENDING, ERROR(")
    assert g.get("N").ended is not None


def test_c28_a_rejected_submission_leaves_earlier_batch_staging_unchanged(
    harness: Harness,
) -> None:
    g = harness.graph
    a = Producer()
    g.node("A", [], a)
    g.subscribe("o", "A")
    rejected: list[str] = []

    def body() -> None:
        a.down([(DATA, 1)])
        try:
            a.down([("RESOLVED",)])
        except KernelError as error:
            rejected.append(str(error))
        a.down([(COMPLETE,)])
        try:
            a.down([(DATA, 2)])
        except KernelError as error:
            rejected.append(str(error))

    assert during(g, lambda: g.batch(body)) == ["o <- A: [PENDING, DATA(1), COMPLETE]"]
    assert len(rejected) == 2


def test_c28_a_batch_opened_from_a_callback_commits_after_the_wave(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 0)
    g.state("B", 0)
    returned: list[object] = []

    def o1(array: tuple[Any, ...]) -> None:
        if array == (("PENDING",), (DATA, 1)):

            def body() -> str:
                g.set("B", g.get("A").value)
                g.set("B", 10)
                return "body result"

            returned.append(g.batch(body))

    g.subscribe("o1", "A", o1)
    g.subscribe("o2", "A")
    g.subscribe("b", "B")
    assert during(g, lambda: g.set("A", 1)) == [
        "o1 <- A: [PENDING, DATA(1)]",
        "o2 <- A: [PENDING, DATA(1)]",
        "b <- B: [PENDING, DATA(1), DATA(10)]",
    ]
    assert returned == ["body result"]


def test_c28_a_sink_failure_after_commit_rolls_nothing_back() -> None:
    reports: list[Report] = []
    g = Graph(report=reports.append)
    g.state("a", 1)
    g.node("d", ["a"], map_fn(lambda v: v * 2))

    def o(array: tuple[Any, ...]) -> None:
        if array[0][0] == "PENDING":
            raise RuntimeError("observer")

    g.subscribe("o", "d", o)
    g.batch(lambda: g.set("a", 5))
    assert (g.get("a").value, g.get("d").value) == (5, 10)
    assert [(r.source, str(r.error)) for r in reports] == [("sink o", "observer")]


# ---- Batch basics (R-request-order; the rest is qualified with C-29 in phase 5) ----


def test_batch_returns_the_body_result_and_a_throwing_body_commits_nothing(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("A", 0)
    g.subscribe("a", "A")
    assert during(g, lambda: g.batch(lambda: 7)) == []
    assert g.batch(lambda: 7) == 7

    def failing() -> None:
        g.set("A", 1)
        raise ValueError("body failed")

    with pytest.raises(ValueError, match="body failed"):
        during(g, lambda: g.batch(failing))
    assert g.get("A").values == (0,)


def test_batch_refuses_an_async_body_before_it_runs(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 0)
    ran: list[int] = []

    async def body() -> None:
        ran.append(1)

    with pytest.raises(KernelError, match="async function"):
        g.batch(body)  # type: ignore[unused-coroutine]
    assert ran == []


def test_a_nested_batch_joins_the_outer_one(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 0)
    g.state("B", 0)
    g.subscribe("a", "A")
    g.subscribe("b", "B")

    def outer() -> str:
        g.set("A", 1)

        def set_b() -> str:
            g.set("B", 2)
            return "inner"

        inner = g.batch(set_b)
        g.set("A", 3)
        return inner

    assert during(g, lambda: g.batch(outer)) == [
        "a <- A: [PENDING, DATA(1), DATA(3)]",
        "b <- B: [PENDING, DATA(2)]",
    ]


# ---- C-41: the snapshot is the node's last array that carried DATA -----------


def test_c41_the_snapshot_is_the_last_array_that_carried_data(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 0)
    plan: dict[int, list[Any]] = {1: [(DATA, 1)], 2: [(DATA, 2), (DATA, 3)], 3: []}

    def n(ctx: Ctx) -> None:
        out = plan.get(ctx.deps[0].value)  # type: ignore[call-overload]
        if out:
            ctx.down(out)

    g.node("N", ["A"], n)
    g.subscribe("s", "N")
    for step in (1, 2, 3):
        g.set("A", step)  # 3 sends nothing: the kernel settles N with RESOLVED
    assert during(g, lambda: g.subscribe("late", "N")) == ["late <- N: [START, DATA(2), DATA(3)]"]

    first: list[list[list[object]]] = []
    g.node("M", ["N"], lambda ctx: first.append(ctx.deps[0].waves))
    g.subscribe("m", "M")
    assert first == [[[2, 3]]]

    plan[4] = [(INVALIDATE,)]
    g.set("A", 4)
    assert during(g, lambda: g.subscribe("after", "N")) == ["after <- N: [START]"]


def test_c41_a_batch_is_one_array_and_reactivation_clears_a_computed_node(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("Q", 0)
    g.state("A", 1)
    g.node("N", ["A"], echo)
    g.subscribe("q", "Q")
    g.subscribe("n", "N")

    def two() -> None:
        g.set("Q", 1)
        g.set("Q", 2)

    g.batch(two)
    assert during(g, lambda: g.subscribe("q2", "Q")) == ["q2 <- Q: [START, DATA(1), DATA(2)]"]

    for sink, node in (("q", "Q"), ("q2", "Q"), ("n", "N")):
        g.unsubscribe(sink, node)
    assert g.get("N").has is False and g.get("N").active is False
    assert g.get("Q").values == (1, 2)  # a state keeps its values
    assert during(g, lambda: g.subscribe("q3", "Q")) == ["q3 <- Q: [START, DATA(1), DATA(2)]"]
    assert during(g, lambda: g.subscribe("n2", "N")) == ["n2 <- N: [START, DATA(1)]"]  # ran again


# ---- C-44: the kernel adds only the settling RESOLVED; a quiet node never holds a wave ----


def test_c44_value_and_quiet_clauses(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 1)

    def f(ctx: Ctx) -> None:
        value = ctx.deps[0].value
        assert isinstance(value, int)
        if value % 2 == 0:
            ctx.down([(DATA, value)])

    kept: list[Ctx] = []

    def q(ctx: Ctx) -> None:
        kept.append(ctx)
        if ctx.deps[0].value == 2:
            ctx.down([(DATA, "in-run")])

    g.node("F", ["A"], f)
    g.node("Q", ["A"], q, quiet=True)
    g.node("D", ["A", "Q"], lambda ctx: ctx.down([(DATA, [d.value for d in ctx.deps])]))
    g.subscribe("sf", "F")
    g.subscribe("sq", "Q")
    g.subscribe("sd", "D")

    assert during(g, lambda: g.set("A", 3)) == [
        "sf <- F: [PENDING, RESOLVED]",
        "sd <- D: [PENDING, DATA([3,null])]",
    ]
    assert during(g, lambda: g.set("A", 2)) == [
        "sf <- F: [PENDING, DATA(2)]",
        "sd <- D: [PENDING, DATA([2,null])]",
        'sq <- Q: [PENDING, DATA("in-run")]',
        'sd <- D: [PENDING, DATA([2,"in-run"])]',
    ]
    assert during(g, lambda: kept[-1].down([(DATA, "late")])) == [
        'sq <- Q: [PENDING, DATA("late")]',
        'sd <- D: [PENDING, DATA([2,"late"])]',
    ]
    assert during(g, lambda: g.set("A", 5)) == [
        "sf <- F: [PENDING, RESOLVED]",
        'sd <- D: [PENDING, DATA([5,"late"])]',
    ]
    assert during(g, lambda: g.invalidate("A")) == [
        "sf <- F: [PENDING, INVALIDATE]",
        "sq <- Q: [PENDING, INVALIDATE]",
        "sd <- D: [PENDING, INVALIDATE]",
    ]


# ---- Dep.sent_data, the facade gate's fast read of ``any(dep.waves)`` ----


@pytest.mark.parametrize("invalidate", ["auto", "manual"])
def test_sent_data_is_any_wave_with_a_value(harness: Harness, invalidate: Any) -> None:
    g = harness.graph
    a, b = Producer(), Producer()
    g.node("A", [], a)
    g.node("B", [], b)

    def odd(ctx: Ctx) -> None:  # an even value sends nothing, so W gets a RESOLVED
        out = [(DATA, v) for wave in ctx.deps[0].waves for v in wave if cast(int, v) % 2]
        if out:
            ctx.down(out)

    g.node("M", ["A"], odd)
    seen: list[tuple[bool, bool]] = []

    def watch(ctx: Ctx) -> None:
        for dep in ctx.deps:
            seen.append((dep.sent_data, any(dep.waves)))

    g.node("W", ["M", "B"], watch, invalidate=invalidate, complete="manual")
    harness.sink("W")

    def invalidate_then_value() -> None:  # one wave: an INVALIDATE, then a value
        b.down([(INVALIDATE,)])
        b.down([(DATA, 6)])

    steps: list[Callable[[], object]] = [
        lambda: a.down([(DATA, 1)]),
        lambda: b.down([(DATA, None)]),
        lambda: a.down([(DATA, 2)]),  # RESOLVED from M
        lambda: a.down([(DATA, 3), (DATA, 5)]),
        lambda: b.down([(INVALIDATE,)]),
        lambda: b.down([(DATA, 4)]),
        lambda: g.batch(invalidate_then_value),
        lambda: a.down([(COMPLETE,)]),
    ]
    every: set[tuple[bool, bool]] = set()
    for step in steps:
        seen.clear()
        step()
        assert all(fast == slow for fast, slow in seen), seen
        every |= set(seen)
    assert every == {(True, True), (False, False)}  # both answers were exercised
