"""Phase 5: request order, batch, set with a function, replace, ctx.rewire, definition.

Scenarios C-6, C-11, C-29, C-35, C-37 and the replace clause of C-40 of graphrefly
spec/conformance.jsonl, qualified against graphrefly._kernel (graphrefly-py:D2), plus
the replace, rewire, seed and definition behavior of R-request-order, R-node and
R-subscription.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from conftest import Harness, Producer, during

from graphrefly._kernel import COMPLETE, DATA, ERROR, Ctx, Graph, KernelError, Seed
from graphrefly._sentinel import SENTINEL


def echo(ctx: Ctx) -> None:
    if ctx.deps[0].has:
        ctx.down([(DATA, ctx.deps[0].value)])


def sum_by_id(ctx: Ctx) -> None:
    ctx.down([(DATA, ",".join(f"{d.id}={d.value}" for d in ctx.deps))])


class Kept:
    """Wraps a node function, counting its runs and keeping the latest ctx."""

    def __init__(self, fn: Callable[[Ctx], None]) -> None:
        self.fn = fn
        self.calls = 0
        self.ctx: Ctx | None = None

    def __call__(self, ctx: Ctx) -> None:
        self.calls += 1
        self.ctx = ctx
        self.fn(ctx)

    def rewire(self, deps: list[str]) -> None:
        assert self.ctx is not None
        self.ctx.rewire(deps)


def deps_of(g: Graph, node_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    current = next(d for d in g.describe() if d.id == node_id).deps
    return current, g.definition(node_id).deps


# ---- C-6: a function's write to its own upstream is a later request ------------


def test_c6_a_write_to_upstream_is_a_later_request_never_a_reentry(harness: Harness) -> None:
    g = harness.graph
    g.state("S", 0)
    g.node("D", ["S"], lambda ctx: ctx.down([(DATA, ctx.deps[0].value + 1)]))  # type: ignore[operator]
    running = [False]
    e_runs: list[object] = []

    def e(ctx: Ctx) -> None:
        assert not running[0], "reentered"
        running[0] = True
        try:
            value = ctx.deps[0].value
            e_runs.append(value)
            assert isinstance(value, int)
            if value < 3:
                g.set("S", value)  # waits until this wave ends
        finally:
            running[0] = False

    def scan(ctx: Ctx) -> None:
        total = (ctx.state if isinstance(ctx.state, int) else 0) + 1
        ctx.state = total
        ctx.down([(DATA, total)])

    g.node("E", ["D"], e)
    g.node("scan", ["S"], scan)
    g.subscribe("e", "E")
    g.subscribe("scan", "scan")
    e_runs.clear()
    g.set("S", 0)
    assert e_runs == [1, 2, 3]  # S: 0 (D=1) -> 1 (D=2) -> 2 (D=3), then E stops
    assert g.get("S").values == (2,)
    assert g.get("scan").values == (4,)  # one run per wave: activation and three writes
    assert harness.reports == []


# ---- C-11: a node rewires itself from its function, for its lifetime -----------


def test_c11_an_operator_rewires_itself_to_each_inner_for_its_lifetime(harness: Harness) -> None:
    g = harness.graph
    g.state("S")
    log: list[str] = []
    inners: dict[str, Producer] = {}
    for name in ("a", "b"):
        inners[name] = Producer()
        g.node(
            f"inner_{name}",
            [],
            inners[name],
            setup=lambda ctx, n=name: lambda: log.append(f"cleanup inner_{n}"),  # type: ignore[misc]
        )
    contexts: list[Ctx] = []
    run_deps: list[tuple[str, ...]] = []

    def op(ctx: Ctx) -> None:
        contexts.append(ctx)
        run_deps.append(tuple(d.id for d in ctx.deps))
        count = (ctx.state if isinstance(ctx.state, int) else 0) + 1
        ctx.state = count
        for wave in ctx.deps[0].waves:
            for v in wave:
                ctx.rewire(["S", f"inner_{v}"])
        for dep in ctx.deps[1:]:
            for wave in dep.waves:
                if wave:
                    ctx.down([(DATA, x) for x in wave])

    g.node("OP", ["S"], op)
    g.subscribe("op", "OP")
    first_ctx = contexts[-1]

    assert during(g, lambda: g.set("S", "a")) == ["op <- OP: [PENDING, RESOLVED]"]
    assert deps_of(g, "OP") == (("S", "inner_a"), ("S",))
    assert during(g, lambda: inners["a"].down([(DATA, "a1")])) == [
        'op <- OP: [PENDING, DATA("a1")]'
    ]

    assert during(g, lambda: g.set("S", "b")) == ["op <- OP: [PENDING, RESOLVED]"]
    assert log == ["cleanup inner_a"]  # released by the rewire to [S, inner_b]
    assert deps_of(g, "OP") == (("S", "inner_b"), ("S",))
    assert g.get("OP").values == ("a1",)  # value kept across the rewires
    assert all(c.signal is first_ctx.signal for c in contexts)  # one lifetime
    assert contexts[-1].state == len(contexts)  # private state kept

    g.unsubscribe("op", "OP")
    assert log == ["cleanup inner_a", "cleanup inner_b"]
    assert deps_of(g, "OP") == (("S",), ("S",))
    runs = len(run_deps)
    g.subscribe("op2", "OP")
    # The new lifetime starts on [S]; its first run sees S = b and rewires again.
    assert run_deps[runs] == ("S",)
    assert contexts[runs].state == 1 and contexts[runs].signal is not first_ctx.signal
    assert deps_of(g, "OP") == (("S", "inner_b"), ("S",))


# ---- C-29: a batch delivers nothing before commit ------------------------------


def _c29(harness: Harness) -> tuple[Graph, Kept]:
    g = harness.graph
    g.state("A", 1)
    b = Kept(sum_by_id)
    g.node("B", ["A"], b)
    g.state("C", "c")
    g.subscribe("s", "B")
    g.subscribe("c", "C")
    return g, b


def test_c29_nothing_changes_in_the_body_and_requests_follow_the_commit(harness: Harness) -> None:
    g, b = _c29(harness)
    reads: list[object] = []

    def body() -> None:
        g.set("A", 2)
        g.subscribe("t", "B")
        b.rewire(["A", "C"])
        reads.append(g.get("A").value)

    assert during(g, lambda: g.batch(body)) == [
        's <- B: [PENDING, DATA("A=2")]',
        't <- B: [START, DATA("A=2")]',
        's <- B: [PENDING, DATA("A=2,C=c")]',
        't <- B: [PENDING, DATA("A=2,C=c")]',
    ]
    assert reads == [1]


def test_c29_requests_of_the_body_run_before_the_commit_waves_own(harness: Harness) -> None:
    g, b = _c29(harness)
    order: list[str] = []

    def s_sink(array: tuple[Any, ...]) -> None:
        if array == (("PENDING",), (DATA, "A=2")):
            g.subscribe("late", "C", lambda a: order.append("commit-wave request"))

    g.subscribe("s2", "B", s_sink)

    def body() -> None:
        g.set("A", 2)
        g.subscribe("t", "C", lambda a: order.append("body request"))

    g.batch(body)
    assert order == ["body request", "commit-wave request"]


def test_c29_a_throwing_body_delivers_nothing_and_drops_its_requests(harness: Harness) -> None:
    g, b = _c29(harness)

    def body() -> None:
        g.set("A", 2)
        g.subscribe("t", "B")
        raise ValueError("body failed")

    lines: list[str] = []
    with pytest.raises(ValueError):
        lines = during(g, lambda: g.batch(body))
    assert lines == [] and len(harness.graph.trace or []) == 2
    assert g.get("A").values == (1,)
    assert all(e.to != "t" for e in g.trace or [])


def test_c29_async_and_awaitable_returning_bodies(harness: Harness) -> None:
    g, _ = _c29(harness)
    ran: list[int] = []

    async def async_body() -> None:
        ran.append(1)

    with pytest.raises(KernelError, match="async function"):
        g.batch(async_body)  # type: ignore[unused-coroutine]
    assert ran == []

    async def later() -> None:
        g.set("A", 3)

    def body() -> Any:
        g.set("A", 2)
        return later()

    lines: list[str] = []
    awaitable: Any = None

    def run() -> None:
        nonlocal awaitable
        awaitable = g.batch(body)

    lines = during(g, run)
    assert lines == ['s <- B: [PENDING, DATA("A=2")]']
    assert [r.source for r in harness.reports] == ["batch"]
    assert during(g, lambda: asyncio.run(awaitable)) == ['s <- B: [PENDING, DATA("A=3")]']


def test_c29_a_nested_batch_joins_the_outer_one_and_returns_its_value(harness: Harness) -> None:
    g, _ = _c29(harness)
    returned: list[object] = []

    def inner() -> str:
        g.set("C", "c2")
        return "inner value"

    def outer() -> None:
        g.set("A", 2)
        returned.append(g.batch(inner))

    assert during(g, lambda: g.batch(outer)) == [
        's <- B: [PENDING, DATA("A=2")]',
        'c <- C: [PENDING, DATA("c2")]',
    ]
    assert returned == ["inner value"]


def test_c29_set_functions_run_at_the_commit_in_staging_order(harness: Harness) -> None:
    g, _ = _c29(harness)
    seen: list[object] = []

    def plus(n: int) -> Callable[[object], object]:
        def fn(prev: object) -> object:
            seen.append(prev)
            return (0 if prev is SENTINEL else prev) + n  # type: ignore[operator]

        return fn

    def body() -> None:
        g.set("A", plus(10))
        g.set("A", 5)
        g.set("A", plus(100))
        g.invalidate("A")
        g.set("A", plus(1000))
        assert seen == []  # nothing runs before the commit

    assert during(g, lambda: g.batch(body)) == [
        "s <- B: [PENDING, INVALIDATE]",
        's <- B: [PENDING, DATA("A=1000")]',
    ]
    assert seen == [1, 5, SENTINEL]  # retained, the value set before, no value


def test_c29_a_throwing_set_function_fails_the_whole_commit_but_requests_run(
    harness: Harness,
) -> None:
    g, b = _c29(harness)

    def failing(prev: object) -> object:
        raise RuntimeError("set fn failed")

    def body() -> None:
        g.set("C", "c2")
        g.set("A", failing)
        g.subscribe("t", "C")  # a topology request of the body still runs (graphrefly:D799)

    lines: list[str] = []
    with pytest.raises(RuntimeError, match="set fn failed"):
        g.batch(body)
    assert g.get("C").values == ("c",) and g.get("A").values == (1,)
    assert any(e.to == "t" for e in g.trace or [])
    del lines


def test_c29_a_set_function_returning_its_argument_writes_nothing(harness: Harness) -> None:
    g, _ = _c29(harness)
    assert during(g, lambda: g.set("A", lambda prev: prev)) == []
    assert during(g, lambda: g.set("A", 1)) == ['s <- B: [PENDING, DATA("A=1")]']
    wrapped = print
    g.set("A", lambda prev: wrapped)  # a callable is written as a value only wrapped
    assert g.get("A").value is print


def test_c29_set_functions_queued_in_one_wave_both_apply(harness: Harness) -> None:
    g = harness.graph
    g.state("locks", ())
    g.state("a", 1)

    def sink(array: tuple[Any, ...]) -> None:
        if array == (("PENDING",), (DATA, 2)):
            g.set("locks", lambda prev: (*prev, "x"))
            g.set("locks", lambda prev: (*prev, "y"))

    g.subscribe("s", "a", sink)
    g.set("a", 2)
    assert g.get("locks").value == ("x", "y")


def test_c29_a_batch_from_a_callback_runs_its_body_at_once(harness: Harness) -> None:
    g, _ = _c29(harness)
    results: list[object] = []
    first = [True]

    def s2(array: tuple[Any, ...]) -> None:
        if first[0] and array[0] == ("PENDING",):
            first[0] = False

            def body() -> object:
                read = g.get("A").value
                g.set("A", 100)
                return read

            results.append(g.batch(body))

    g.subscribe("s2", "B", s2)
    g.subscribe("s3", "B")
    assert during(g, lambda: g.set("A", 2)) == [
        's <- B: [PENDING, DATA("A=2")]',
        's2 <- B: [PENDING, DATA("A=2")]',
        's3 <- B: [PENDING, DATA("A=2")]',
        's <- B: [PENDING, DATA("A=100")]',
        's2 <- B: [PENDING, DATA("A=100")]',
        's3 <- B: [PENDING, DATA("A=100")]',
    ]
    assert results == [2]


# ---- C-35: rewire keeps the instance and runs between waves --------------------


def test_c35_rewire_keeps_the_instance_and_runs_between_waves(harness: Harness) -> None:
    g = harness.graph
    g.state("B", "b")
    g.state("C", "c")
    g.state("E", "e")
    g.complete("E")
    d = Kept(sum_by_id)
    g.node("D", ["B"], d, complete="manual")
    g.subscribe("s", "D")
    first_signal = d.ctx.signal if d.ctx else None

    assert during(g, lambda: d.rewire(["B", "C"])) == ['s <- D: [PENDING, DATA("B=b,C=c")]']
    calls = d.calls
    assert during(g, lambda: d.rewire(["C", "B"])) == []  # records kept, no run
    assert d.calls == calls
    assert during(g, lambda: g.set("C", "c2")) == ['s <- D: [PENDING, DATA("C=c2,B=b")]']
    assert during(g, lambda: d.rewire(["C"])) == []
    assert g.get("D").values == ("C=c2,B=b",)
    assert during(g, lambda: g.set("B", "b2")) == []  # B no longer drives D
    assert during(g, lambda: d.rewire(["C"])) == []  # to the current list: nothing
    seen_terminal: list[object] = []
    d.fn = lambda ctx: seen_terminal.append([dep.terminal for dep in ctx.deps])
    assert during(g, lambda: d.rewire(["C", "E"])) == []  # D ran and sent nothing
    assert seen_terminal[-1] == [None, (COMPLETE,)]  # E's snapshot carries its COMPLETE
    d.fn = sum_by_id

    g.state("G")
    calls = d.calls
    assert during(g, lambda: d.rewire(["C", "G"])) == []  # G has no snapshot: no run
    assert d.calls == calls
    assert during(g, lambda: g.set("G", "g")) == ['s <- D: [PENDING, DATA("C=c2,G=g")]']
    assert during(g, lambda: d.rewire([])) == []
    assert g.get("D").values == ("C=c2,G=g",)
    assert during(g, lambda: g.set("C", "c3")) == []

    for bad in (["D"], ["C", "C"]):
        with pytest.raises(KernelError):
            d.rewire(bad)
    g.node("after", ["D"], echo)
    with pytest.raises(KernelError, match="depend on itself through after"):
        d.rewire(["after"])

    assert deps_of(g, "D") == ((), ("B",))
    assert d.ctx is not None and d.ctx.signal is first_signal
    old = d.ctx
    g.unsubscribe("s", "D")
    g.subscribe("s", "D")
    assert deps_of(g, "D") == (("B",), ("B",))
    assert during(g, lambda: old.rewire(["C"])) == []  # through the closed lifetime: dropped
    assert deps_of(g, "D") == (("B",), ("B",))

    last = d.ctx
    assert last is not None
    d.fn = lambda ctx: ctx.down([(COMPLETE,)])
    g.set("B", "b3")
    assert g.get("D").ended == (COMPLETE,)
    assert during(g, lambda: last.rewire(["C"])) == []  # D has ended: dropped
    assert deps_of(g, "D") == (("B",), ("B",))


def test_c35_a_rewire_asked_from_a_function_waits_for_the_wave(harness: Harness) -> None:
    g = harness.graph
    g.state("a", 1)
    g.state("b", 2)
    order: list[str] = []

    def fn(ctx: Ctx) -> None:
        order.append(",".join(dep.id for dep in ctx.deps))
        if len(ctx.deps) == 1 and ctx.deps[0].value == 5:
            ctx.rewire(["a", "b"])

    g.node("x", ["a"], fn)
    g.subscribe("s", "x")
    g.set("a", 5)
    assert order == ["a", "a", "a,b"]  # the run that asked finished with the old list


# ---- C-37: a closed lifetime revokes old work while the node and topology remain ---


def test_c37_rewire_keeps_the_lifetime_and_a_closed_one_drops_late_output(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("A", 1)
    g.state("B", 2)
    n = Kept(lambda ctx: None)
    g.node("N", ["A"], n)
    g.subscribe("s", "N")
    work = n.ctx
    assert work is not None
    n.rewire(["A", "B"])
    assert during(g, lambda: work.down([(DATA, "late 1")])) == ['s <- N: [PENDING, DATA("late 1")]']

    new_work = n.ctx
    assert new_work is not None
    g.unsubscribe("s", "N")
    g.subscribe("s", "N")
    assert during(g, lambda: new_work.down([(DATA, "late 2")])) == []
    assert deps_of(g, "N") == (("A",), ("A",))
    assert n.ctx is not new_work
    assert not hasattr(n.ctx, "up")


# ---- C-40: the replace clause ----------------------------------------------------


def test_c40_replace_gives_an_ended_node_a_new_instance_and_its_sink_start_again(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("S", 1)
    g.complete("S")
    g.node("D", ["S"], echo)
    g.subscribe("d", "D")
    assert g.get("D").ended == (COMPLETE,)
    g.state("T", 7)
    assert during(g, lambda: g.replace("D", ["T"], echo)) == ["d <- D: [START, DATA(7)]"]
    assert g.get("D").ended is None
    assert during(g, lambda: g.set("T", 8)) == ["d <- D: [PENDING, DATA(8)]"]


# ---- replace (R-request-order, R-node) ----------------------------------------


def test_replace_sinks_get_start_and_dependents_one_input_wave(harness: Harness) -> None:
    g = harness.graph
    g.state("a", 1)

    def x(ctx: Ctx) -> None:
        value = ctx.deps[0].value
        assert isinstance(value, int)
        ctx.down([(DATA, value)] if value < 2 else [(COMPLETE,)])

    g.node("x", ["a"], x)
    g.node(
        "d",
        ["x"],
        lambda ctx: ctx.down([(DATA, [v for w in ctx.deps[0].waves for v in w])]),
        complete="manual",
    )
    g.subscribe("s", "d")
    g.subscribe("t", "x")
    g.set("a", 2)
    assert g.get("x").ended == (COMPLETE,)
    assert during(
        g,
        lambda: g.replace("x", ["a"], lambda ctx: ctx.down([(DATA, ctx.deps[0].value * 10)])),  # type: ignore[operator]
    ) == ["t <- x: [START, DATA(20)]", "s <- d: [PENDING, DATA([20])]"]
    assert g.get("x").values == (20,) and g.get("x").ended is None
    assert during(g, lambda: g.set("a", 5)) == [
        "s <- d: [PENDING, DATA([50])]",
        "t <- x: [PENDING, DATA(50)]",
    ]


def test_replace_closes_the_old_lifetime_and_gives_the_new_instance_its_own_setup(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("a", 1)
    log: list[str] = []
    late: list[Ctx] = []

    def x(ctx: Ctx) -> None:
        late.append(ctx)
        ctx.signal.on_abort(lambda: log.append("abort"))

    g.node("x", ["a"], x, setup=lambda ctx: lambda: log.append("cleanup"))
    g.subscribe("s", "x")
    g.replace("x", ["a"], lambda ctx: None, setup=lambda ctx: lambda: log.append("cleanup 2"))
    assert log == ["abort", "cleanup"]
    g.unsubscribe("s", "x")
    assert log == ["abort", "cleanup", "cleanup 2"]
    g.replace("x", ["a"], lambda ctx: None)
    g.subscribe("s", "x")
    g.unsubscribe("s", "x")
    assert log == ["abort", "cleanup", "cleanup 2"]  # a replace without setup has none
    assert during(g, lambda: late[-1].down([(DATA, "old")])) == []


def test_replace_acquires_new_dependencies_before_releasing_old_ones(harness: Harness) -> None:
    g = harness.graph
    g.state("a", 1)
    g.state("b", 2)
    log: list[str] = []
    g.node("up", ["a"], echo, setup=lambda ctx: lambda: log.append("up closed"))
    g.node("x", ["up"], echo)
    g.subscribe("s", "x")
    g.replace("x", ["up", "b"], sum_by_id)
    assert log == []
    assert deps_of(g, "x")[0] == ("up", "b")
    g.replace("x", ["b"], echo)
    assert log == ["up closed"]


def test_replace_of_an_inactive_node_only_changes_its_definition(harness: Harness) -> None:
    g = harness.graph
    g.state("a", 1)
    g.node("x", ["a"], lambda ctx: ctx.down([(DATA, 1)]))
    g.replace("x", ["a"], lambda ctx: ctx.down([(DATA, 2)]), recipe={"ref": "two"})
    assert during(g, lambda: g.subscribe("s", "x")) == ["s <- x: [START, DATA(2)]"]
    assert g.definition("x").recipe == {"ref": "two"}


def test_replace_during_a_wave_waits_and_is_refused_for_states_and_cycles(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("a", 1)
    g.node("x", ["a"], echo)

    def s(array: tuple[Any, ...]) -> None:
        if any(len(m) > 1 and m[1] == 2 for m in array):
            g.replace("x", ["a"], lambda ctx: ctx.down([(DATA, -ctx.deps[0].value)]))  # type: ignore[operator]

    g.subscribe("s", "x", s)
    g.subscribe("t", "x")
    assert during(g, lambda: g.set("a", 2)) == [
        "s <- x: [PENDING, DATA(2)]",
        "t <- x: [PENDING, DATA(2)]",
        "s <- x: [START, DATA(-2)]",
        "t <- x: [START, DATA(-2)]",
    ]
    g.node("y", ["x"], echo)
    with pytest.raises(KernelError, match="not a computed node"):
        g.replace("a", [], echo)
    with pytest.raises(KernelError, match="depend on itself"):
        g.replace("x", ["y"], echo)
    with pytest.raises(KernelError, match="distinct"):
        g.replace("x", ["a", "a"], echo)


def test_replace_snapshot_is_one_wave_so_a_diamond_below_settles_once(harness: Harness) -> None:
    g = harness.graph
    g.state("a", 1)
    g.node("x", ["a"], echo)
    g.node("l", ["x"], lambda ctx: ctx.down([(DATA, ctx.deps[0].value + 1)]))  # type: ignore[operator]
    g.node("r", ["x"], lambda ctx: ctx.down([(DATA, ctx.deps[0].value + 2)]))  # type: ignore[operator]
    j = Kept(lambda ctx: ctx.down([(DATA, [dep.value for dep in ctx.deps])]))
    g.node("j", ["l", "r"], j)
    g.subscribe("s", "j")
    j.calls = 0
    assert during(g, lambda: g.replace("x", ["a"], lambda ctx: ctx.down([(DATA, 10)]))) == [
        "s <- j: [PENDING, DATA([11,12])]"
    ]
    assert j.calls == 1


def test_replace_dependents_lose_the_old_value_and_an_ending_instance_releases(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("a", 1)
    g.state("b", 0)
    g.node("x", ["a"], lambda ctx: ctx.down([(DATA, "old")]))
    seen: list[object] = []
    g.node(
        "d", ["x", "b"], lambda ctx: seen.append(ctx.deps[0].value if ctx.deps[0].has else "none")
    )
    g.subscribe("s", "d")
    g.replace("x", ["a"], lambda ctx: None)
    g.set("b", 1)
    assert seen == ["old", "none"]

    log: list[str] = []
    g.node(
        "up",
        ["a"],
        lambda ctx: ctx.down([(DATA, 1)]),
        setup=lambda ctx: lambda: log.append("up closed"),
    )
    g.node("z", ["up"], echo)
    g.subscribe("z", "z")
    assert during(g, lambda: g.replace("z", ["up"], lambda ctx: ctx.down([(COMPLETE,)]))) == [
        "z <- z: [START, COMPLETE]"
    ]
    assert log == ["up closed"]


def test_replace_over_an_ended_dependency_starts_on_its_snapshot(harness: Harness) -> None:
    g = harness.graph
    g.state("a", 1)
    g.state("done", 0)
    g.complete("done")
    g.node("x", ["a"], lambda ctx: None)
    g.subscribe("s", "x")
    assert during(g, lambda: g.replace("x", ["done"], sum_by_id)) == [
        's <- x: [START, DATA("done=0"), COMPLETE]'
    ]


# ---- rewire (R-request-order) ------------------------------------------------


def test_rewire_keeps_value_private_state_lifetime_and_sinks(harness: Harness) -> None:
    g = harness.graph
    g.state("a", 1)
    g.state("b")
    log: list[str] = []

    def fn(ctx: Ctx) -> None:
        ctx.state = (ctx.state if isinstance(ctx.state, int) else 0) + 1
        ctx.signal.on_abort(lambda: log.append("abort"))
        ctx.down([(DATA, ctx.state)])

    x = Kept(fn)
    g.node("x", ["a"], x)
    g.subscribe("s", "x")
    assert during(g, lambda: x.rewire(["a", "b"])) == []
    assert g.get("x").values == (1,)
    assert during(g, lambda: g.set("b", 0)) == ["s <- x: [PENDING, DATA(2)]"]
    assert log == []


def test_rewire_added_values_are_one_input_wave(harness: Harness) -> None:
    g = harness.graph
    g.state("a", 1)
    g.state("b", 2)
    g.state("c", 3)
    seen: list[list[list[object]]] = []
    x = Kept(lambda ctx: seen.append([[v for w in d.waves for v in w] for d in ctx.deps]))
    g.node("x", ["a"], x)
    g.subscribe("s", "x")
    x.rewire(["a", "b", "c"])
    assert seen[-1] == [[], [2], [3]] and len(seen) == 2


def test_rewire_a_diamond_below_settles_once(harness: Harness) -> None:
    g = harness.graph
    g.state("a", 1)
    g.state("b", 2)
    x = Kept(sum_by_id)
    g.node("x", ["a"], x)
    g.node("l", ["x"], lambda ctx: ctx.down([(DATA, f"l:{ctx.deps[0].value}")]))
    g.node("r", ["x"], lambda ctx: ctx.down([(DATA, f"r:{ctx.deps[0].value}")]))
    join = Kept(lambda ctx: ctx.down([(DATA, "|".join(str(d.value) for d in ctx.deps))]))
    g.node("j", ["l", "r"], join)
    g.subscribe("s", "j")
    before = join.calls
    x.rewire(["a", "b"])
    assert join.calls == before + 1
    assert g.get("j").value == "l:a=1,b=2|r:a=1,b=2"


def test_rewire_acquires_before_releasing_so_a_shared_upstream_stays(harness: Harness) -> None:
    g = harness.graph
    g.state("a", 1)
    log: list[str] = []
    g.node("up", ["a"], echo, setup=lambda ctx: lambda: log.append("up closed"))
    g.node("p", ["up"], echo)
    g.node("q", ["up"], echo)
    x = Kept(sum_by_id)
    g.node("x", ["p"], x)
    g.subscribe("s", "x")
    x.rewire(["q"])
    assert log == [] and g.get("p").active is False and g.get("x").value == "q=1"
    assert g.definition("x").fn is x


def test_rewire_an_added_dependency_that_ends_or_completes_as_it_activates(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("a", 1)
    g.node("once", [], lambda ctx: ctx.down([(DATA, 9), (ERROR, "boom")]))
    x = Kept(sum_by_id)
    g.node("x", ["a"], x)
    g.subscribe("s", "x")
    x.rewire(["a", "once"])
    assert g.get("x").ended == (ERROR, "boom") and g.get("a").active is False

    g.node("empty", [], lambda ctx: ctx.down([(COMPLETE,)]))
    y = Kept(lambda ctx: None)
    g.node("y", ["a"], y, complete="manual")
    g.subscribe("t", "y")
    assert during(g, lambda: y.rewire(["a", "empty"])) == []
    assert y.calls == 2  # a manual terminal is input


def test_rewire_removing_the_last_open_dependency_does_not_auto_complete(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("a", 1)
    g.state("b", 2)
    d = Kept(sum_by_id)
    g.node("d", ["a", "b"], d)
    g.subscribe("s", "d")
    g.complete("a")
    assert during(g, lambda: d.rewire(["a"])) == []
    assert g.get("d").ended is None


# ---- seed, definition, describe (R-subscription, R-node) ----------------------


def test_a_seed_gives_values_state_deps_and_a_note_to_the_first_run_only(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("a", 1)
    g.state("b", 2)
    seen: list[tuple[Any, Any, tuple[str, ...]]] = []

    def fn(ctx: Ctx) -> None:
        seen.append((ctx.seed, ctx.state, tuple(d.id for d in ctx.deps)))

    seed = Seed(values=(10, 11), state="restored", deps=("b",), note={"first": True})
    g.node("x", ["a"], fn, seed=seed)
    assert g.get("x").values == (10, 11)
    assert during(g, lambda: g.subscribe("s", "x")) == ["s <- x: [START, DATA(10), DATA(11)]"]
    assert seen == [({"first": True}, "restored", ("b",))]
    g.set("b", 3)
    assert seen[-1] == (None, "restored", ("b",))
    g.unsubscribe("s", "x")
    g.subscribe("s", "x")
    note, state, deps = seen.pop()
    assert note is None and state is None  # no note and fresh private state
    assert deps == ("a",)  # the declared dependencies

    g.node("ended", ["a"], fn, seed=Seed(values=(5,), ended=(COMPLETE,)))
    runs = len(seen)
    assert during(g, lambda: g.subscribe("e", "ended")) == [
        "e <- ended: [START, DATA(5), COMPLETE]"
    ]
    assert len(seen) == runs  # a seeded ended node never runs
    with pytest.raises(KernelError):
        g.node("bad", ["a"], fn, seed=Seed(ended=(DATA, 1)))


def test_definition_reads_the_declared_definition_and_describe_the_current_deps(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("a", 1)
    g.state("b", 2)

    def setup(ctx: Ctx) -> None:
        return None

    x = Kept(sum_by_id)
    g.node("x", ["a"], x, setup=setup, recipe={"ref": "sum"}, complete="manual", quiet=True)
    g.subscribe("s", "x")
    x.rewire(["a", "b"])
    definition = g.definition("x")
    assert (definition.deps, definition.fn, definition.setup, definition.recipe) == (
        ("a",),
        x,
        setup,
        {"ref": "sum"},
    )
    assert (definition.complete, definition.error, definition.invalidate, definition.quiet) == (
        "manual",
        "auto",
        "auto",
        True,
    )
    assert [(d.id, d.kind, d.deps, d.quiet) for d in g.describe()] == [
        ("a", "state", (), False),
        ("b", "state", (), False),
        ("x", "node", ("a", "b"), True),
    ]
    assert g.definition("a").fn is None


def test_the_cycle_check_sees_declared_dependencies_too(harness: Harness) -> None:
    g = harness.graph
    g.state("a", 1)
    p = Kept(sum_by_id)
    g.node("p", ["a"], p)
    g.subscribe("ps", "p")  # keeps p's lifetime open after q stops reading it
    g.node("q", ["p"], sum_by_id)
    q_ctx: list[Ctx] = []
    g.replace("q", ["p"], lambda ctx: (q_ctx.append(ctx), sum_by_id(ctx)))  # type: ignore[func-returns-value]
    g.subscribe("s", "q")
    q_ctx[-1].rewire(["a"])  # q now reads a, but still declares p
    with pytest.raises(KernelError, match="depend on itself through q"):
        p.rewire(["a", "q"])  # q's declared p would close a cycle once q's lifetime closes


def test_a_queued_rewire_is_dropped_when_its_lifetime_closes_before_it_runs(
    harness: Harness,
) -> None:
    """R-request-order: a rewire is dropped if its lifetime has closed when it runs."""

    g = harness.graph
    g.state("a", 0)
    g.state("b", 1)

    def fn(ctx: Ctx) -> None:
        if ctx.deps[0].value:
            ctx.rewire(["a", "b"])  # queued behind this wave
            ctx.down([(COMPLETE,)])  # the node ends in this wave, closing its lifetime

    g.node("x", ["a"], fn)
    g.subscribe("s", "x")
    g.set("a", 1)
    assert g.get("x").ended == (COMPLETE,)
    assert deps_of(g, "x") == (("a",), ("a",))
    assert g.get("b").active is False


def test_a_replace_is_checked_again_when_it_runs_so_a_batch_cannot_close_a_cycle(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("S", 1)
    g.node("A", ["S"], echo)
    g.node("B", ["S"], echo)

    def body() -> None:
        g.replace("A", ["B"], echo)  # each is valid when asked for
        g.replace("B", ["A"], echo)  # but not after the first one ran

    g.batch(body)
    assert g.definition("A").deps == ("B",) and g.definition("B").deps == ("S",)
    assert [(r.source, str(r.error)) for r in harness.reports] == [
        ("queued request", "replace: B would depend on itself through A")
    ]


def test_a_rewire_is_checked_again_when_it_runs_so_a_batch_cannot_close_a_cycle(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("S", 1)
    a, b = Kept(echo), Kept(echo)
    g.node("A", ["S"], a)
    g.node("B", ["S"], b)
    harness.sink("A")
    harness.sink("B")

    def body() -> None:
        assert a.ctx is not None and b.ctx is not None
        a.ctx.rewire(["B"])
        b.ctx.rewire(["A"])

    g.batch(body)
    assert [d.deps for d in g.describe()] == [(), ("B",), ("S",)]
    assert [(r.source, str(r.error)) for r in harness.reports] == [
        ("queued request", "rewire: B would depend on itself through A")
    ]


def test_two_rewires_from_one_sink_callback_cannot_close_a_cycle(harness: Harness) -> None:
    """C-35 (graphrefly:D806): both are asked for in one wave, each acyclic alone; the
    second is refused when it runs, changes nothing and goes to the reporter."""

    g = harness.graph
    g.state("S", 1)
    a, b = Kept(echo), Kept(echo)
    g.node("A", ["S"], a)
    g.node("B", ["S"], b)
    harness.sink("A")
    harness.sink("B")
    asked = [False]

    def ask(array: tuple[Any, ...]) -> None:
        if array[0] == ("PENDING",) and not asked[0]:
            asked[0] = True
            assert a.ctx is not None and b.ctx is not None
            a.ctx.rewire(["B"])
            b.ctx.rewire(["A"])

    g.subscribe("asker", "S", ask)
    g.set("S", 2)
    assert [d.deps for d in g.describe()] == [(), ("B",), ("S",)]
    assert [(r.source, str(r.error)) for r in harness.reports] == [
        ("queued request", "rewire: B would depend on itself through A")
    ]


def test_staging_many_writes_in_one_batch_is_linear(harness: Harness) -> None:
    """Each staged write is checked against what is staged without copying it: 100,000
    sets in one batch take a fraction of a second, where re-checking the whole staged
    array on every set took about five minutes."""

    import time

    g = harness.graph
    g.state("S", 0)
    sink = harness.sink("S")
    count = 100_000

    def body() -> None:
        for value in range(count):
            g.set("S", value)

    started = time.perf_counter()
    g.batch(body)
    assert time.perf_counter() - started < 10.0
    assert len(sink.arrays[-1]) == count + 1  # PENDING and every value, one array
    assert g.get("S").value == count - 1


def test_a_write_after_a_staged_terminal_is_refused_and_the_staging_kept(
    harness: Harness,
) -> None:
    g = harness.graph
    g.state("S", 0)
    sink = harness.sink("S")

    def body() -> None:
        g.set("S", 1)
        g.complete("S")
        with pytest.raises(KernelError, match=r"S: a terminal must be the last .* \(batch\)"):
            g.set("S", 2)

    g.batch(body)
    assert sink.arrays[-1] == (("PENDING",), (DATA, 1), (COMPLETE,))
