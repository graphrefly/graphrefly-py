"""Phase 1: the closed message set and the checks on node output.

Scenarios C-26, C-42 (as revised by graphrefly:D801), C-12 (c) and C-23 (f) of
graphrefly spec/conformance.jsonl,
qualified against graphrefly._kernel (graphrefly-py:D2). Also the failure
semantics of graphrefly-py:D2: the kernel catches Exception only.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import Harness

from graphrefly._kernel import (
    COMPLETE,
    DATA,
    ERROR,
    INVALIDATE,
    MESSAGE_TYPES,
    PENDING,
    RESOLVED,
    START,
    Ctx,
    Graph,
    KernelError,
)
from graphrefly._sentinel import SENTINEL

RETIRED = [("PULL",), ("PAUSE", "L"), ("RESUME", "L"), ("TEARDOWN",)]


def test_the_message_set_is_the_seven_kernel_types() -> None:
    assert frozenset({START, PENDING, DATA, RESOLVED, INVALIDATE, COMPLETE, ERROR}) == MESSAGE_TYPES
    for retired in ("PULL", "PAUSE", "RESUME", "TEARDOWN", "DIRTY"):
        assert retired not in MESSAGE_TYPES


@pytest.mark.parametrize("retired", RETIRED, ids=lambda m: m[0])
def test_c26_retired_type_in_output_ends_the_node_and_reaches_no_sink(
    harness: Harness, retired: tuple[str, ...]
) -> None:
    """C-26: PULL, PAUSE, RESUME and TEARDOWN are unknown types; the first ends N with ERROR."""

    g = harness.graph
    g.state("A", 0)

    def n(ctx: Ctx) -> None:
        if ctx.deps[0].value:
            ctx.down([retired])
        ctx.down([(DATA, "after")])

    g.node("N", ["A"], n)
    sink = harness.sink("N")
    g.set("A", 1)

    assert sink.arrays[0] == ((START,), (DATA, "after"))
    pending, error = sink.arrays[1]
    assert pending == (PENDING,)
    assert error[0] == ERROR
    assert isinstance(error[1], KernelError)
    assert "unknown message type" in str(error[1])
    assert len(sink.arrays) == 2  # nothing of the rejected array, nor the later DATA
    assert not any(m[0] == retired[0] for array in sink.arrays for m in array)


def test_c26_the_first_retired_type_ends_the_node_so_later_ones_never_run(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("A", 0)
    runs: list[int] = []

    def n(ctx: Ctx) -> None:
        step = ctx.deps[0].value
        assert isinstance(step, int)
        runs.append(step)
        if step:
            ctx.down([RETIRED[step - 1]])
        ctx.down([(DATA, step)])

    g.node("N", ["A"], n)
    sink = harness.sink("N")
    for step in (1, 2, 3, 4):
        g.set("A", step)

    assert runs == [0, 1]
    ended = g.get("N").ended
    assert ended is not None and ended[0] == ERROR
    assert sink.kinds() == [[START, DATA], [PENDING, ERROR]]


def test_c42_each_rejection_applies_nothing_and_later_data_is_retained(
    harness: Harness,
) -> None:
    """C-42 (as revised by graphrefly:D801): PAUSE, RESUME, PULL, TEARDOWN, PENDING, START
    and unknown types are rejected before anything is applied; the DATA sent afterwards
    is retained."""

    g = harness.graph
    g.state("A", 1)
    rejected: list[str] = []
    bad: list[list[Any]] = [
        [("PAUSE", "L")],
        [("RESUME", "L")],
        [("PULL",)],
        [("TEARDOWN",)],
        [(PENDING,)],
        [(DATA, 0), (PENDING,)],
        [(START,)],
        [(DATA, 0), (START,)],
        [("NOT-A-TYPE",)],
        [(DATA, 0), ("NOT-A-TYPE", 1)],
    ]
    seen_ctx: list[Ctx] = []

    def n(ctx: Ctx) -> None:
        seen_ctx.append(ctx)
        for array in bad:
            try:
                ctx.down(array)
            except KernelError as error:
                rejected.append(str(error))
        ctx.down([(DATA, "kept")])

    g.node("N", ["A"], n)
    sink = harness.sink("N")

    assert len(rejected) == len(bad)
    assert g.get("N").values == ("kept",)
    assert sink.arrays == [((START,), (DATA, "kept"))]
    # Nothing travels upstream: ctx has no upward call.
    for name in ("up", "up_next", "upNext"):
        assert not hasattr(seen_ctx[0], name)


def test_c42_uncaught_rejection_is_the_nodes_error(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 1)
    g.node("N", ["A"], lambda ctx: ctx.down([("PAUSE", "L")]))
    sink = harness.sink("N")

    ((start, error),) = sink.arrays
    assert start == (START,)
    assert error[0] == ERROR and isinstance(error[1], KernelError)
    assert g.get("N").ended == error


@pytest.mark.parametrize("kind", [START, PENDING, RESOLVED])
def test_kernel_only_types_are_rejected_in_node_output(harness: Harness, kind: str) -> None:
    """START, PENDING and RESOLVED are the kernel's alone (R-failure@3, graphrefly:D801)."""

    g = harness.graph
    g.state("A", 1)
    caught: list[KernelError] = []

    def n(ctx: Ctx) -> None:
        try:
            ctx.down([(DATA, 0), (kind,)])
        except KernelError as error:
            caught.append(error)
        ctx.down([(DATA, "kept")])

    g.node("N", ["A"], n)
    sink = harness.sink("N")
    assert len(caught) == 1 and f"{kind} is sent by the kernel" in str(caught[0])
    assert sink.arrays == [((START,), (DATA, "kept"))]


def test_c12c_resolved_in_node_output_is_rejected_and_ends_the_node(harness: Harness) -> None:
    """C-12 (c): RESOLVED is the kernel's alone."""

    g = harness.graph
    g.state("A", 0)

    def n(ctx: Ctx) -> None:
        if ctx.deps[0].value:
            ctx.down([(RESOLVED,)])

    g.node("N", ["A"], n)
    sink = harness.sink("N")
    g.set("A", 1)

    assert sink.arrays[0] == ((START,),)
    pending, error = sink.arrays[1]
    assert pending == (PENDING,) and error[0] == ERROR
    assert "RESOLVED is sent by the kernel" in str(error[1])
    assert g.get("N").ended == error


def test_c23f_data_sentinel_is_rejected(harness: Harness) -> None:
    """C-23 (f): DATA SENTINEL is rejected, from a node and from the host."""

    g = harness.graph
    g.state("A", 1)
    caught: list[KernelError] = []

    def n(ctx: Ctx) -> None:
        try:
            ctx.down([(DATA, SENTINEL)])
        except KernelError as error:
            caught.append(error)

    g.node("N", ["A"], n)
    sink = harness.sink("N")

    assert len(caught) == 1 and "SENTINEL" in str(caught[0])
    assert sink.arrays == [((START,),)]
    with pytest.raises(KernelError, match="SENTINEL"):
        g.set("A", SENTINEL)
    with pytest.raises(KernelError, match="SENTINEL"):
        g.state("B", SENTINEL)
    assert g.get("A").values == (1,)


@pytest.mark.parametrize(
    "array",
    [
        [(ERROR, SENTINEL)],
        [(ERROR, True)],
        [(ERROR,)],
        [(DATA,)],
        [(COMPLETE, 1)],
        [(COMPLETE,), (DATA, 1)],
        [(ERROR, "e"), (DATA, 1)],
        [(START, 1)],
        ["DATA"],
        [()],
    ],
    ids=repr,
)
def test_malformed_output_is_rejected_before_anything_is_applied(
    harness: Harness, array: list[Any]
) -> None:
    g = harness.graph
    g.state("A", 1)
    caught: list[KernelError] = []

    def n(ctx: Ctx) -> None:
        try:
            ctx.down(array)
        except KernelError as error:
            caught.append(error)

    g.node("N", ["A"], n)
    harness.sink("N")
    assert len(caught) == 1
    assert g.get("N").has is False and g.get("N").ended is None


def test_down_takes_a_list_of_messages_not_a_bare_tuple(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 1)
    caught: list[KernelError] = []

    def n(ctx: Ctx) -> None:
        try:
            ctx.down((DATA, 1))  # type: ignore[arg-type]
        except KernelError as error:
            caught.append(error)

    g.node("N", ["A"], n)
    harness.sink("N")
    assert len(caught) == 1


def test_error_payload_may_be_any_non_bool_diagnostic(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 1)
    g.node("N", ["A"], lambda ctx: ctx.down([(ERROR, {"code": 1})]))
    sink = harness.sink("N")
    assert sink.arrays == [((START,), (ERROR, {"code": 1}))]
    with pytest.raises(KernelError, match="bool"):
        g.error("A", False)


# ---- Failure semantics (graphrefly-py:D2) -----------------------------------


def test_a_function_exception_is_its_nodes_error_with_the_exception_as_payload(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("A", 1)
    boom = ValueError("boom")

    def n(ctx: Ctx) -> None:
        ctx.down([(DATA, "dropped")])
        raise boom

    g.node("N", ["A"], n)
    sink = harness.sink("N")
    assert sink.arrays == [((START,), (ERROR, boom))]


def test_a_sink_exception_goes_to_the_reporter_and_is_not_a_node_error(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("A", 1)
    g.node("N", ["A"], lambda ctx: ctx.down([(DATA, ctx.deps[0].value)]))
    later = harness.sink("N", "later")

    def failing(_array: tuple[Any, ...]) -> None:
        raise RuntimeError("sink failed")

    g.subscribe("failing", "N", failing)
    g.set("A", 2)

    assert [r.source for r in harness.reports] == ["sink failing", "sink failing"]
    assert g.get("N").ended is None
    assert later.arrays[-1] == ((PENDING,), (DATA, 2))


@pytest.mark.parametrize(
    "fatal", [KeyboardInterrupt, SystemExit, GeneratorExit, RecursionError, MemoryError]
)
def test_a_fatal_from_a_function_propagates_and_the_kernel_stays_usable(
    fatal: type[BaseException],
) -> None:
    reports: list[Any] = []
    g = Graph(report=reports.append)
    g.state("A", 0)
    armed = [False]

    def n(ctx: Ctx) -> None:
        if armed[0]:
            armed[0] = False
            raise fatal()
        ctx.down([(DATA, ctx.deps[0].value)])

    g.node("N", ["A"], n)
    m_inputs: list[list[list[object]]] = []

    def m(ctx: Ctx) -> None:
        m_inputs.append(ctx.deps[0].waves)
        ctx.down([(DATA, ctx.deps[1].value)])

    g.node("M", ["A", "N"], m)
    seen: list[tuple[Any, ...]] = []
    g.subscribe("s", "M", seen.append)

    armed[0] = True
    with pytest.raises(fatal):
        g.set("A", 1)

    # Neither an ERROR nor a report; the interrupted wave's pending marks are reset.
    assert g.get("N").ended is None and g.get("M").ended is None
    assert reports == []
    assert not g._busy
    g.set("A", 2)
    assert seen[-1] == ((PENDING,), (DATA, 2))
    assert m_inputs[-1] == [[2]]  # nothing left over from the interrupted wave


@pytest.mark.parametrize("fatal", [KeyboardInterrupt, RecursionError])
def test_a_fatal_from_a_sink_propagates_and_is_not_reported(
    harness: Harness, fatal: type[BaseException]
) -> None:
    g = harness.graph
    g.state("A", 0)

    def fatal_sink(array: tuple[Any, ...]) -> None:
        if array[0] != (START,):
            raise fatal()

    g.subscribe("s", "A", fatal_sink)
    with pytest.raises(fatal):
        g.set("A", 1)
    assert harness.reports == []
    g.unsubscribe("s", "A")
    g.set("A", 2)
    assert g.get("A").values == (2,)


def test_a_fatal_drops_the_requests_queued_behind_it(harness: Harness) -> None:
    g = harness.graph
    g.state("A", 0)
    g.state("B", 0)

    def sink(array: tuple[Any, ...]) -> None:
        if array == ((PENDING,), (DATA, 1)):
            g.set("B", 99)  # queued behind the running wave
            raise SystemExit

    g.subscribe("s", "A", sink)
    with pytest.raises(SystemExit):
        g.set("A", 1)
    assert g.get("B").values == (0,)
    g.unsubscribe("s", "A")
    g.set("A", 2)  # a later request does not run the dropped one
    assert g.get("B").values == (0,)


def test_a_reporter_exception_is_swallowed_but_a_fatal_one_propagates() -> None:
    def failing_sink(array: tuple[Any, ...]) -> None:
        if array[0] != (START,):
            raise ValueError("sink failed")

    def bad_reporter(_report: Any) -> None:
        raise RuntimeError("reporter failed")

    g = Graph(report=bad_reporter)
    g.state("A", 0)
    g.subscribe("s", "A", failing_sink)
    g.set("A", 1)  # the sink's failure reaches a failing reporter; nothing escapes

    def fatal_reporter(_report: Any) -> None:
        raise KeyboardInterrupt

    g2 = Graph(report=fatal_reporter)
    g2.state("A", 0)
    g2.subscribe("s", "A", failing_sink)
    with pytest.raises(KeyboardInterrupt):
        g2.set("A", 1)
