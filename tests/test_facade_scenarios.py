"""The kernel scenarios through the public facade (graphrefly-py:D797-KERNEL-PY).

tests/kernel qualifies each scenario against the private kernel, in the ids and arrays
the scenario text uses. These tests drive the same scenarios through the public API
only: State for host input, Graph.node and its Ctx for node output, subscribe for
sinks. A clause the public API cannot express is left to the kernel test and named
in the test's docstring: the facade has no host RESOLVED and no host array, by design.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from typing import Any

import pytest

from graphrefly import (
    COMPLETE,
    DATA,
    ERROR,
    INVALIDATE,
    PENDING,
    RESOLVED,
    SENTINEL,
    START,
    Ctx,
    Graph,
    GraphCallbackError,
    GraphEvent,
    GraphReflyRuntimeError,
    GraphReflyValueError,
    HostReport,
    Node,
    Recipe,
    RecipeContext,
    State,
    restore_graph,
)

Arrays = list[tuple[Any, ...]]


def echo(ctx: Ctx) -> None:
    """Send the last value of each array the first dependency brought."""

    for wave in ctx.deps[0].waves:
        if wave:
            ctx.emit(wave[-1])


def sink(node: Node[Any]) -> Arrays:
    seen: Arrays = []
    node.subscribe(seen.append)
    return seen


class Sinks:
    """Sinks whose subscriptions a test can close together, through the public handles."""

    def __init__(self) -> None:
        self.subscriptions: list[Any] = []

    def on(self, node: Node[Any]) -> Arrays:
        seen: Arrays = []
        self.subscriptions.append(node.subscribe(seen.append))
        return seen

    def close(self) -> None:
        for subscription in self.subscriptions:
            subscription.unsubscribe()
        self.subscriptions.clear()


def kinds(ctx: Ctx) -> list[str]:
    return [dep.terminal[0] if dep.terminal else "-" for dep in ctx.deps]


class Kept:
    """A node callback that keeps its latest ctx, so later output is its own wave."""

    def __init__(self, body: Callable[[Ctx], None] | None = None) -> None:
        self.body = body
        self.ctx: Ctx | None = None
        self.ctxs: list[Ctx] = []

    def __call__(self, ctx: Ctx) -> None:
        if not self.ctxs or self.ctxs[-1] is not ctx:
            self.ctxs.append(ctx)
        self.ctx = ctx
        if self.body is not None:
            self.body(ctx)


# ---- C-3: INVALIDATE through a diamond --------------------------------------------


@pytest.mark.parametrize("policy", ["auto", "manual"])
def test_c3_one_invalidate_out_private_state_kept_policy_decides(policy: str) -> None:
    g = Graph("c3")
    s = g.state(1, name="S")
    b = g.node([s], echo, name="B")
    c = g.node([s], echo, name="C")
    runs: list[list[bool]] = []

    def d(ctx: Ctx) -> None:
        deps = ctx.deps
        runs.append([dep.invalidated for dep in deps])
        if any(dep.invalidated for dep in deps):
            ctx.down([(INVALIDATE,)])  # manual: the function decides what to clear
            return
        if all(dep.has for dep in deps):
            ctx.state = (ctx.state or 0) + 1
            ctx.emit((deps[0].value, deps[1].value, ctx.state))

    dn = g.node([b, c], d, name="D", invalidate=policy)  # type: ignore[arg-type]
    seen = sink(dn)
    s.set(2)
    assert dn.cache() == (2, 2, 2)
    runs.clear()
    seen.clear()
    s.invalidate()
    assert seen == [((PENDING,), (INVALIDATE,))]  # one INVALIDATE, both legs in one wave
    assert not dn.has_value
    assert runs == ([] if policy == "auto" else [[True, True]])
    s.set(3)
    assert dn.cache() == (3, 3, 3)  # the private state was kept: the count goes on
    s.invalidate()
    seen.clear()
    s.invalidate()  # S holds no value: the INVALIDATE is dropped
    assert seen == []
    s.set(4)
    assert dn.cache() == (4, 4, 4)


# ---- C-4: a sync leg and an async leg ----------------------------------------------


def test_c4_the_async_result_is_a_later_wave_and_nothing_waits_across_it() -> None:
    g = Graph("c4")
    a = g.state(0, name="A")
    b = g.node([a], echo, name="B")
    work = Kept()  # starts work and sends nothing in its run
    cn = g.node([a], work, name="C")
    d_runs: list[tuple[object, object]] = []
    dn = g.node([b, cn], lambda ctx: d_runs.append((ctx.deps[0].value, ctx.deps[1].value)))
    sinks = Sinks()
    c_seen = sinks.on(cn)
    sinks.on(dn)
    c_seen.clear()
    d_runs.clear()
    a.set(1)
    assert c_seen == [((PENDING,), (RESOLVED,))]  # C settles with the kernel's RESOLVED
    assert d_runs == [(1, SENTINEL)] and dn.status.cache != "stale"
    assert work.ctx is not None
    work.ctx.emit("r1")  # the work finishes: a new wave from C
    assert d_runs == [(1, SENTINEL), (1, "r1")]
    a.set(2)
    late = work.ctx
    sinks.close()  # C's lifetime closes before the second result arrives
    assert cn.status.lifecycle == "inactive"
    late.emit("r2")  # its lifetime closed: dropped
    assert not cn.has_value and len(d_runs) == 3


# ---- C-6: a write to its own upstream is a later request ---------------------------


def test_c6_a_write_to_upstream_is_a_later_request_never_a_reentry() -> None:
    g = Graph("c6")
    s = g.state(name="S")
    d = g.derived([s], lambda n: n + 1, name="D")
    entered = [0]
    depth = [0]

    def e(ctx: Ctx) -> None:
        depth[0] += 1
        entered[0] = max(entered[0], depth[0])
        value = ctx.deps[0].value
        if value is not SENTINEL and value < 3:
            s.set(value)  # waits until the wave ends
        depth[0] -= 1

    def scan(ctx: Ctx) -> None:
        total = ctx.state or 0
        for wave in ctx.deps[0].waves:
            total += sum(wave)
        ctx.state = total
        ctx.emit(total)

    en = g.node([d], e, name="E")
    scanned = g.node([s], scan, name="scan")
    scan_seen = sink(scanned)
    en.subscribe(lambda _: None)
    s.set(0)
    assert d.cache() == 3 and entered[0] == 1  # never reentered
    # S takes 0, 1, 2; the scan runs once per wave.
    assert [a[-1] for a in scan_seen[1:]] == [(DATA, 0), (DATA, 1), (DATA, 3)]


# ---- C-11: a node rewires itself from its function ---------------------------------


def test_c11_an_operator_rewires_itself_to_each_inner_for_its_lifetime() -> None:
    g = Graph("c11")
    s = g.state(name="S")
    cleaned: list[str] = []
    inners: dict[str, tuple[Node[Any], Kept]] = {}
    for key in ("a", "b"):
        kept = Kept()

        def setup(ctx: Ctx, key: str = key) -> Callable[[], None]:
            return lambda: cleaned.append(key)

        inners[key] = (g.node([], kept, name=f"inner_{key}", setup=setup), kept)
    seen_deps: list[list[str]] = []
    states: list[object] = []

    def op(ctx: Ctx) -> None:
        seen_deps.append([dep.id for dep in ctx.deps])
        for wave in ctx.deps[0].waves:
            for key in wave:
                ctx.rewire([s, inners[key][0]])
        for dep in ctx.deps[1:]:
            for wave in dep.waves:
                for value in wave:
                    ctx.emit(value)
        ctx.state = (ctx.state or 0) + 1
        states.append(ctx.state)

    opn = g.node([s], op, name="OP")
    sub = opn.subscribe(lambda _: None)
    s.set("a")
    assert [d["deps"] for d in g.describe() if d["id"] == "OP"] == [["S", "inner_a"]]
    inner_a = inners["a"][1].ctx
    assert inner_a is not None
    inner_a.emit("x")
    assert opn.cache() == "x"
    s.set("b")
    assert cleaned == ["a"]  # inner a, released by the rewire, deactivated
    assert opn.cache() == "x"  # the value is kept across the rewire
    inner_b = inners["b"][1].ctx
    assert inner_b is not None
    inner_b.emit("y")
    assert opn.cache() == "y" and states == sorted(states)  # private state went on
    assert g.definition(opn).deps == (s,)  # the declared dependencies never changed
    sub.unsubscribe()  # the lifetime closes: back to the declared [S]
    assert cleaned == ["a", "b"]
    seen_deps.clear()
    opn.subscribe(lambda _: None)
    assert seen_deps[0] == ["S"]  # the new lifetime starts on [S]


# ---- C-12: occurrences stay DATA ---------------------------------------------------


def test_c12_equal_values_stay_data_a_silent_node_settles_and_resolved_is_rejected() -> None:
    g = Graph("c12")
    source = Kept()
    src = g.node([], source, name="src")
    seen = sink(src)
    assert source.ctx is not None
    for _ in range(3):
        source.ctx.emit(1)
    source.ctx.down([(COMPLETE,)])
    assert seen[1:] == [((PENDING,), (DATA, 1))] * 3 + [((PENDING,), (COMPLETE,))]

    s = g.state(0, name="s")
    silent = g.node([s], lambda ctx: None, name="silent")
    below = g.node([silent], lambda ctx: None, name="below")
    silent_seen = sink(silent)
    below.subscribe(lambda _: None)
    for value in (1, 2, 3):
        s.set(value)
        assert below.status.cache != "stale"
    assert silent_seen[1:] == [((PENDING,), (RESOLVED,))] * 3

    resolves = g.node([s], lambda ctx: ctx.down([(RESOLVED,)]), name="resolves")
    errors = sink(resolves)
    assert errors[0][1][0] == ERROR and isinstance(errors[0][1][1], GraphCallbackError)


# ---- C-15: a terminal settles its pending mark --------------------------------------


def _c15() -> tuple[Graph, State[Any], State[Any], list[tuple[list[str], object]], Arrays]:
    g = Graph("c15")
    a = g.state(1, name="A")
    a2 = g.state(1, name="A2")
    b = g.node([a], echo, name="B")
    c = g.node([a2], echo, name="C")
    runs: list[tuple[list[str], object]] = []
    d = g.node(
        [b, c],
        lambda ctx: runs.append((kinds(ctx), ctx.deps[1].waves)),
        name="D",
        complete="manual",
        error="manual",
    )
    seen = sink(d)
    runs.clear()
    seen.clear()
    return g, a, a2, runs, seen


def test_c15_a_dependencys_terminal_settles_its_pending_mark() -> None:
    g, a, a2, runs, _seen = _c15()

    def end_a_and_write_a2() -> None:
        a.complete()
        a2.set(2)

    g.batch(end_a_and_write_a2)
    assert runs == [(["COMPLETE", "-"], [[2]])]  # one run: B's terminal and C's value

    g, a, _a2, runs, seen = _c15()
    a.complete()
    assert runs == [(["COMPLETE", "-"], [])]  # the terminal alone
    assert seen == [((PENDING,), (RESOLVED,))]  # it sent nothing: one RESOLVED

    g, a, a2, runs, _seen = _c15()

    def error_a_and_write_a2() -> None:
        a.error("e")
        a2.set(3)

    g.batch(error_a_and_write_a2)
    assert runs == [(["ERROR", "-"], [[3]])]


# ---- C-16: a quiet node absorbs its input waves -------------------------------------


def test_c16_a_quiet_node_absorbs_its_waves_and_marks_its_cone_on_invalidate() -> None:
    g = Graph("c16")
    a = g.state(0, name="A")
    held: list[object] = []
    q = Kept(lambda ctx: held.extend(v for wave in ctx.deps[0].waves for v in wave))
    qn = g.node([a], q, name="Q", quiet=True)
    e_runs: list[list[list[object]]] = []
    en = g.node([qn], lambda ctx: e_runs.append(ctx.deps[0].waves), name="E")
    b = g.node([qn], echo, name="B")
    c = g.node([qn], echo, name="C")
    f_runs: list[list[bool]] = []
    fn = g.node(
        [b, c],
        lambda ctx: f_runs.append([dep.invalidated for dep in ctx.deps]),
        name="F",
        invalidate="manual",
    )
    q_seen = sink(qn)
    en.subscribe(lambda _: None)
    fn.subscribe(lambda _: None)
    e_runs.clear()
    a.set(1)
    a.set(2)
    assert q_seen == [((START,),)] and e_runs == []  # not even PENDING
    assert en.status.cache != "stale"
    assert q.ctx is not None
    q.ctx.emit(*held)  # after its function returned: a later wave
    assert q_seen[-1] == ((PENDING,), (DATA, 0), (DATA, 1), (DATA, 2))
    assert e_runs == [[[0, 1, 2]]]
    assert sink(qn) == [((START,), (DATA, 0), (DATA, 1), (DATA, 2))]
    f_runs.clear()
    a.invalidate()
    assert q_seen[-1] == ((PENDING,), (INVALIDATE,))
    assert f_runs == [[True, True]]  # F settled once, seeing both legs


# ---- C-17: an ended dependency counts toward auto COMPLETE ---------------------------


@pytest.mark.parametrize("order", ["error-first", "complete-first"])
def test_c17_auto_complete_after_every_dependency_ended_in_any_order(order: str) -> None:
    g = Graph("c17")
    b = g.state(0, name="B")
    c = g.state(0, name="C")
    runs: list[list[str]] = []
    ctrl_runs: list[int] = []
    d = g.node([b, c], lambda ctx: runs.append(kinds(ctx)), name="D", error="manual")
    ctrl = g.node([b, c], lambda ctx: ctrl_runs.append(1), name="Dctrl")
    d_seen = sink(d)
    ctrl.subscribe(lambda _: None)
    runs.clear()
    ctrl_runs.clear()

    def value_and_complete() -> None:
        b.set(1)
        b.complete()

    if order == "error-first":
        c.error("e")
        assert runs == [["-", "ERROR"]] and d.status.output == "open"
        assert ctrl_runs == [] and ctrl.status.output == "errored"
        g.batch(value_and_complete)
    else:
        g.batch(value_and_complete)
        before = len(ctrl_runs)
        c.error("e")
        assert len(ctrl_runs) == before and ctrl.status.output == "errored"
    assert runs[-1] in (["COMPLETE", "-"], ["-", "ERROR"])
    assert d.status.output == "completed"
    assert sum(1 for array in d_seen if array[-1] == (COMPLETE,)) == 1


def test_c17_only_the_last_terminal_alone_runs_the_function_then_completes() -> None:
    g = Graph("c17-last")
    b = g.state(1, name="B")
    c = g.state(2, name="C")
    runs: list[list[str]] = []
    n = g.node([b, c], lambda ctx: runs.append(kinds(ctx)), name="N")
    seen = sink(n)
    runs.clear()
    b.complete()  # an earlier terminal alone does not run it
    assert runs == [] and seen[-1] == ((PENDING,), (RESOLVED,))
    c.complete()  # the last one, alone, does; then N completes (graphrefly:D806)
    assert runs == [["-", "COMPLETE"]] and seen[-1] == ((PENDING,), (COMPLETE,))
    assert n.status.output == "completed"
    g.close()


# ---- C-23: ctx.deps is the function's only input --------------------------------------


def test_c23_ctx_deps_shows_arrays_terminal_and_latest_value() -> None:
    g = Graph("c23")
    x = g.state(0, name="X")
    plan: list[Callable[[Ctx], None]] = []

    def a(ctx: Ctx) -> None:
        if plan:
            plan.pop(0)(ctx)
        elif ctx.deps[0].waves:
            ctx.emit(ctx.deps[0].value)

    an = g.node([x], a, name="A")
    b = g.state(name="B")
    shown: list[list[tuple[str, list[list[object]], object, object]]] = []

    def n(ctx: Ctx) -> None:
        shown.append([(dep.id, dep.waves, dep.terminal, dep.value) for dep in ctx.deps])

    nn = g.node([an, b], n, name="N", complete="manual", error="manual")
    nn.subscribe(lambda _: None)
    b.set("b1")  # (a) B only
    assert shown[-1][0] == ("A", [], None, 0) and shown[-1][1] == ("B", [["b1"]], None, "b1")
    plan.append(lambda ctx: None)
    x.set(1)  # (b) A sends RESOLVED alone
    assert shown[-1][0] == ("A", [[]], None, 0)
    plan.append(lambda ctx: ctx.down([(DATA, 1), (DATA, 2), (INVALIDATE,), (DATA, 3)]))
    x.set(2)  # (c) split at the last INVALIDATE
    assert shown[-1][0] == ("A", [[3]], None, 3)
    plan.append(lambda ctx: ctx.emit(None))
    x.set(3)
    plan.append(lambda ctx: ctx.emit([]))
    x.set(4)  # (d) None and [] are ordinary payloads
    assert [s[0][1] for s in shown[-2:]] == [[[None]], [[[]]]]
    plan.append(lambda ctx: ctx.down([(COMPLETE,)]))
    x.set(5)
    b.error("e")  # (e) the terminals, never among the arrays
    assert shown[-2][0][1:3] == ([], (COMPLETE,)) and shown[-1][1][1:3] == ([], (ERROR, "e"))
    bad = g.node([x], lambda ctx: ctx.emit(SENTINEL), name="bad")
    assert sink(bad)[0][1][0] == ERROR  # (f) DATA SENTINEL is rejected


# ---- C-26 and C-42: the message set is closed ----------------------------------------


@pytest.mark.parametrize(
    "array",
    [
        [("PULL",)],
        [("PAUSE", "L")],
        [("RESUME", "L")],
        [("TEARDOWN",)],
        [(PENDING,)],
        [(START,)],
        [("WHATEVER",)],
    ],
)
def test_c26_c42_a_message_outside_the_set_is_rejected_before_anything_applies(
    array: list[tuple[object, ...]],
) -> None:
    g = Graph("closed")
    a = g.state(1, name="A")
    caught: list[str] = []

    def n(ctx: Ctx) -> None:
        try:
            ctx.down(array)  # type: ignore[arg-type]
        except GraphReflyValueError as error:
            caught.append(str(error))
        ctx.emit("after")

    node = g.node([a], n, name="N")
    assert sink(node) == [((START,), (DATA, "after"))] and len(caught) == 1
    uncaught = g.node([a], lambda ctx: ctx.down(array), name="U")  # type: ignore[arg-type]
    seen = sink(uncaught)
    assert seen[0][1][0] == ERROR and isinstance(seen[0][1][1], GraphCallbackError)
    assert not hasattr(Ctx, "up")  # nothing travels upstream


# ---- C-28: intact output arrays and written order -----------------------------------------


def test_c28_arrays_stay_intact_and_requests_from_sinks_wait_for_the_fanout() -> None:
    reports: list[HostReport] = []
    g = Graph("c28", report=reports.append)
    x = g.state(0, name="X")
    plan: list[Callable[[Ctx], None]] = []
    an = g.node([x], lambda ctx: plan.pop(0)(ctx) if plan else None, name="A")
    b = g.node([an], echo, name="B")
    c = g.node([an], echo, name="C")
    d_runs: list[list[list[list[object]]]] = []
    d = g.node([b, c], lambda ctx: d_runs.append([dep.waves for dep in ctx.deps]), name="D")
    first, second = sink(an), sink(an)
    d.subscribe(lambda _: None)
    d_runs.clear()
    plan.append(lambda ctx: ctx.emit(23, 23, 30))
    x.set(1)
    assert first[-1] == second[-1] == ((PENDING,), (DATA, 23), (DATA, 23), (DATA, 30))
    assert d_runs == [[[[30]], [[30]]]]  # once, after B and C settled

    def three(ctx: Ctx) -> None:
        for value in (1, 2, 3):
            ctx.emit(value)

    plan.append(three)
    x.set(2)
    assert first[-3:] == [((PENDING,), (DATA, v)) for v in (1, 2, 3)]
    plan.append(lambda ctx: ctx.down([(DATA, 1), (INVALIDATE,), (DATA, 2), (DATA, 3)]))
    x.set(3)
    assert first[-2:] == [((PENDING,), (INVALIDATE,)), ((PENDING,), (DATA, 2), (DATA, 3))]

    order: list[str] = []
    y = g.state(0, name="Y")

    def asks(array: tuple[Any, ...]) -> None:
        if array == ((PENDING,), (DATA, 1)):
            y.set(9)  # a request: after the whole fanout
            order.append("asked")

    y.subscribe(asks)
    y.subscribe(lambda array: order.append(f"second {array[-1]}"))
    y.set(1)
    assert order == ["second ('DATA', 0)", "asked", "second ('DATA', 1)", "second ('DATA', 9)"]

    s = g.state(0, name="S")
    s_seen = sink(s)

    def body() -> None:
        s.set(1)
        s.complete()
        with pytest.raises(GraphReflyRuntimeError):
            s.set(2)  # after a COMPLETE: refused, the staging stays as it was

    g.batch(body)
    assert s_seen[-1] == ((PENDING,), (DATA, 1), (COMPLETE,))

    z = g.state(0, name="Z")

    def failing(array: tuple[Any, ...]) -> None:
        if array[0] == (PENDING,):
            raise RuntimeError("sink failed after the commit")

    z.subscribe(failing)
    g.batch(lambda: z.set(5))
    assert z.cache() == 5 and [r.source.split()[0] for r in reports] == ["sink"]

    for array in ([(PENDING,), (DATA, 1)], [(DATA, 2), (RESOLVED,)], [(RESOLVED,)]):
        bad = g.node([x], lambda ctx, array=array: ctx.down(array))  # type: ignore[misc]
        assert sink(bad)[0][1][0] == ERROR


# ---- C-29: a batch delivers nothing before commit -----------------------------------------


def test_c29_a_batch_commits_once_and_its_topology_requests_follow() -> None:
    reports: list[HostReport] = []
    g = Graph("c29", report=reports.append)
    a = g.state(0, name="A")
    c = g.state("c", name="C")
    kept = Kept(echo)
    b = g.node([a], kept, name="B")
    s_seen = sink(b)
    t_seen: Arrays = []
    reads: list[object] = []

    def body() -> None:
        a.set(1)
        b.subscribe(t_seen.append)
        assert kept.ctx is not None
        kept.ctx.rewire([a, c])
        reads.append(a.cache())
        assert s_seen == [((START,), (DATA, 0))]  # nothing delivered during the body

    g.batch(body)
    assert reads == [0] and s_seen[-1] == ((PENDING,), (DATA, 1))
    assert t_seen == [((START,), (DATA, 1))]  # t starts from the committed value
    assert [n["deps"] for n in g.describe() if n["id"] == "B"] == [["A", "C"]]

    def throws() -> None:
        a.set(99)
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        g.batch(throws)
    assert a.cache() == 1 and s_seen[-1] == ((PENDING,), (DATA, 1))

    async def async_body() -> None:
        a.set(5)

    with pytest.raises(GraphReflyRuntimeError):
        g.batch(async_body)  # type: ignore[arg-type]
    assert a.cache() == 1
    assert g.batch(lambda: g.batch(lambda: a.set(2)) or "outer") == "outer"
    assert a.cache() == 2

    seen_by: list[object] = []

    def staged() -> None:
        a.set(lambda v: seen_by.append(v) or v + 1)
        a.set(10)
        a.set(lambda v: seen_by.append(v) or v + 1)
        a.invalidate()
        a.set(lambda v: seen_by.append(v) or 7)

    g.batch(staged)
    assert seen_by == [2, 10, SENTINEL] and a.cache() == 7
    before = len(s_seen)
    a.set(lambda v: v)  # returns what it was given: no wave
    assert len(s_seen) == before
    a.set(7)  # a plain equal value is written
    assert len(s_seen) == before + 1


# ---- C-33: a closed lifetime revokes old ctx ---------------------------------------------


def test_c33_a_closed_lifetime_revokes_old_ctx_and_cleanup_failures_stay_isolated() -> None:
    reports: list[HostReport] = []
    g = Graph("c33", report=reports.append)
    a = g.state(1, name="A")
    order: list[str] = []
    p = g.node([a], echo, name="P", setup=lambda ctx: lambda: order.append("P cleanup"))
    statuses: list[str] = []

    def n_setup(ctx: Ctx) -> Callable[[], None]:
        order.append("N setup")

        def cleanup() -> None:
            order.append(f"N cleanup read {ctx.state}")
            statuses.append(nn.status.lifecycle)
            raise RuntimeError("cleanup failed")

        return cleanup

    def remember(ctx: Ctx) -> None:
        if ctx.deps[0].has:
            ctx.state = ctx.deps[0].value  # the value it ran on
            ctx.emit(ctx.deps[0].value)

    work = Kept(remember)
    nn = g.node([p], work, name="N", setup=n_setup)
    assert (nn.status.lifecycle, nn.status.cache) == ("inactive", "empty")
    sub = nn.subscribe(lambda _: None)
    assert (nn.status.lifecycle, nn.status.cache) == ("active", "ready")
    a.set(2)
    assert order == ["N setup"]  # the second run runs no setup
    a.invalidate()
    assert nn.status.cache == "empty"
    a.set(3)
    old = work.ctx
    assert old is not None
    signal = old.signal
    sub.unsubscribe()
    assert order == ["N setup", "P cleanup", "N cleanup read 3"]  # dependencies first
    assert statuses == ["closing"] and signal.aborted
    assert [r.source for r in reports] == ["cleanup N"]
    old.emit("late")  # dropped
    assert not nn.has_value and old.state is None
    nn.subscribe(lambda _: None)
    assert work.ctx is not old and order[-1] == "N setup"
    a.complete()
    assert nn.status.output == "completed"


# ---- C-34: terminal fanout and the ended node -----------------------------------------------


def test_c34_terminal_fanout_closes_the_lifetime_and_an_ended_node_gives_its_snapshot() -> None:
    reports: list[HostReport] = []
    g = Graph("c34", report=reports.append)
    t = g.state(name="T")
    cleaned: list[str] = []

    def s_fn(ctx: Ctx) -> None:
        for wave in ctx.deps[0].waves:
            if wave:
                ctx.down([(DATA, wave[-1]), (COMPLETE,)])

    s = g.node([t], s_fn, name="S", setup=lambda ctx: lambda: cleaned.append("S"))
    z_seen: Arrays = []
    x_seen: Arrays = []

    def x(array: tuple[Any, ...]) -> None:
        x_seen.append(array)
        if array[0] == (PENDING,):
            s.subscribe(z_seen.append)
            raise RuntimeError("x failed")

    s.subscribe(x)
    y_seen = sink(s)
    t.set(1)
    assert x_seen[-1] == y_seen[-1] == ((PENDING,), (DATA, 1), (COMPLETE,))
    assert cleaned == ["S"] and t.status.lifecycle == "inactive"  # T released
    assert z_seen == [((START,), (DATA, 1), (COMPLETE,))]
    assert s.cache() == 1 and s.status.output == "completed"
    assert [r.source.split()[0] for r in reports] == ["sink"]  # not S's ERROR


# ---- C-35: rewire keeps the instance -------------------------------------------------------


def test_c35_rewire_keeps_the_instance_and_a_cycle_is_refused_when_it_runs() -> None:
    reports: list[HostReport] = []
    g = Graph("c35", report=reports.append)
    b = g.state(1, name="B")
    c = g.state(2, name="C")
    e = g.state(0, name="E")
    e.complete()
    runs: list[dict[str, list[list[object]]]] = []
    kept = Kept(lambda ctx: runs.append({dep.id: dep.waves for dep in ctx.deps}))
    d = g.node([b], kept, name="D", complete="manual")
    d_seen = sink(d)

    def rewire(*deps: Node[Any]) -> None:
        assert kept.ctx is not None
        kept.ctx.rewire(deps)

    runs.clear()
    rewire(b, c)
    assert runs == [{"B": [], "C": [[2]]}] and d_seen == [((START,),)]  # one input wave
    rewire(c, b)
    rewire(c)
    c.set(3)
    b.set(5)  # B released: it no longer drives D
    assert runs[-1] == {"C": [[3]]}
    rewire(c, e)
    assert runs[-1]["E"] == [[0]]  # E's snapshot, its last value and COMPLETE, is input
    g_ = g.state(name="G")
    count = len(runs)
    rewire(c, e, g_)
    assert len(runs) == count  # G has no snapshot: no run
    rewire()
    b.set(6)
    assert len(runs) == count
    with pytest.raises(GraphReflyValueError):
        rewire(d)
    assert g.definition(d).deps == (b,)

    x = g.node([b], echo, name="X")
    y = g.node([b], echo, name="Y")

    def body() -> None:
        g.replace(x, [y], echo)
        g.replace(y, [x], echo)

    g.batch(body)
    assert g.definition(x).deps == (y,) and g.definition(y).deps == (b,)
    assert [r.source for r in reports] == ["queued request"]

    # Two rewires asked from one sink callback: the second would close the cycle.
    kx, ky = Kept(echo), Kept(echo)
    x2 = g.node([b], kx, name="X2")
    y2 = g.node([b], ky, name="Y2")
    x2.subscribe(lambda _: None)
    y2.subscribe(lambda _: None)
    asked: list[bool] = []

    def ask(array: tuple[Any, ...]) -> None:
        if array[0] == (PENDING,) and not asked:
            asked.append(True)
            assert kx.ctx is not None and ky.ctx is not None
            kx.ctx.rewire([y2])
            ky.ctx.rewire([x2])

    b.subscribe(ask)
    b.set(7)
    deps = {n["id"]: n["deps"] for n in g.describe()}
    assert (deps["X2"], deps["Y2"]) == (["Y2"], ["B"])  # Y2 keeps its dependencies
    assert [r.source for r in reports] == ["queued request", "queued request"]


# ---- C-36: input arrays and terminal admission ----------------------------------------------


@pytest.mark.parametrize("policy", ["manual", "auto"])
def test_c36_one_invocation_sees_a_value_and_its_terminal(policy: str) -> None:
    g = Graph("c36")
    x = g.state(0, name="X")
    plan: list[Callable[[Ctx], None]] = []
    a = g.node([x], lambda ctx: plan.pop(0)(ctx) if plan else None, name="A")
    seen_inputs: list[tuple[list[list[object]], object]] = []

    def d(ctx: Ctx) -> None:
        dep = ctx.deps[0]
        seen_inputs.append((dep.waves, dep.terminal))
        for wave in dep.waves:
            if wave:
                ctx.emit(wave[-1] * 2)

    dn = g.node([a], d, name="d", complete=policy)  # type: ignore[arg-type]
    seen = sink(dn)
    seen_inputs.clear()
    plan.append(lambda ctx: ctx.down([(DATA, 20), (COMPLETE,)]))
    x.set(1)
    assert seen_inputs == [([[20]], (COMPLETE,))]
    if policy == "manual":
        assert dn.status.output == "open"
    else:
        assert seen[-2:] == [((PENDING,), (DATA, 40)), ((PENDING,), (COMPLETE,))]
        assert not any(RESOLVED in [m[0] for m in array] for array in seen)


def test_c36_data_then_invalidate_is_published_as_invalidate() -> None:
    g = Graph("c36b")
    x = g.state(0, name="X")
    plan: list[Callable[[Ctx], None]] = []
    a = g.node([x], lambda ctx: plan.pop(0)(ctx) if plan else ctx.emit(1), name="A")
    runs: list[int] = []
    d = g.node([a], lambda ctx: (runs.append(1), echo(ctx)), name="d")
    a_seen, d_seen = sink(a), sink(d)
    runs.clear()
    plan.append(lambda ctx: ctx.down([(DATA, 7), (INVALIDATE,)]))
    x.set(1)
    assert a_seen[-1] == d_seen[-1] == ((PENDING,), (INVALIDATE,))
    assert runs == [] and not a.has_value and not d.has_value


# ---- C-37: a closed lifetime revokes old work -------------------------------------------------


def test_c37_rewire_keeps_the_lifetime_and_a_closed_one_drops_late_output() -> None:
    g = Graph("c37")
    a = g.state(1, name="A")
    b = g.state(2, name="B")
    kept = Kept()
    n = g.node([a], kept, name="N")
    sinks = Sinks()
    seen = sinks.on(n)
    first = kept.ctx
    assert first is not None
    first.rewire([a, b])
    first.emit("late")  # the lifetime is the same: published
    assert seen[-1] == ((PENDING,), (DATA, "late"))
    sinks.close()
    seen2 = sinks.on(n)
    first.emit("old")  # dropped
    assert all(array[-1] != (DATA, "old") for array in seen2)
    assert kept.ctx is not first
    assert [d["deps"] for d in g.describe() if d["id"] == "N"] == [["A"]]


# ---- C-38: restore keeps the saved state ----------------------------------------------------


def test_c38_restore_runs_no_callback_and_refreshes_once_on_a_changed_input() -> None:
    calls = [0]

    def total(config: object, at: RecipeContext) -> Any:
        def run(ctx: Ctx) -> None:
            calls[0] += 1
            ctx.state = (ctx.state or 0) + 1
            ctx.emit(sum(dep.value for dep in ctx.deps if dep.has))

        return run

    reg = {"total": total}

    def no_make(ref: str, config: object, name: str) -> Any:
        raise AssertionError("total makes no other recipe")

    g = Graph("c38")
    a = g.state(1, name="a")
    p = g.node([a], total(None, RecipeContext(g, "p", no_make)), name="p", recipe=Recipe("total"))
    p.subscribe(lambda _: None)
    a.set(2)
    cp = g.checkpoint()
    calls[0] = 0
    r = restore_graph(cp, registry=reg)
    rp = r.find("p")
    ra = r.find("a")
    assert rp is not None and isinstance(ra, State)
    seen = sink(rp)
    assert seen == [((START,), (DATA, 2))] and calls[0] == 0  # the saved value, no run
    ra.set(5)
    assert calls[0] == 1 and rp.cache() == 5

    local = Graph("c38-local")
    la = local.state(1, name="a")
    local.node([la], echo, name="local")
    with pytest.raises(Exception, match="local-only"):
        restore_graph(local.checkpoint(), registry=reg)


# ---- C-40: an ended node can be subscribed and depended on -----------------------------------


def test_c40_an_ended_node_can_be_subscribed_depended_on_and_replaced() -> None:
    g = Graph("c40")
    s = g.state(1, name="S")
    s.complete()
    t = g.state(name="T")
    t.error("e")
    assert sink(s) == [((START,), (DATA, 1), (COMPLETE,))]
    d_runs: list[list[list[object]]] = []
    d = g.node([s], lambda ctx: d_runs.append(ctx.deps[0].waves), name="D")
    e_runs: list[int] = []
    e = g.node([t], lambda ctx: e_runs.append(1), name="E")
    m_runs: list[list[str]] = []
    m = g.node(
        [t], lambda ctx: m_runs.append(kinds(ctx)), name="M", error="manual", complete="manual"
    )
    d_seen = sink(d)
    e.subscribe(lambda _: None)
    m.subscribe(lambda _: None)
    assert d_runs == [[[1]]] and d.status.output == "completed"
    assert e_runs == [] and e.status.output == "errored"
    assert m_runs == [["ERROR"]] and m.status.output == "open"
    with pytest.raises(GraphReflyRuntimeError, match="ended"):
        s.set(2)
    g.replace(d, [s], lambda ctx: ctx.emit("new"))
    assert d_seen[-1] == ((START,), (DATA, "new"), (COMPLETE,))


# ---- C-41: the snapshot is the last array that carried DATA ------------------------------------


def test_c41_the_snapshot_is_the_last_array_that_carried_data() -> None:
    g = Graph("c41")
    x = g.state(0, name="X")
    plan: list[Callable[[Ctx], None]] = [lambda ctx: ctx.emit(1)]
    n = g.node([x], lambda ctx: plan.pop(0)(ctx) if plan else None, name="N")
    sinks = Sinks()
    sinks.on(n)
    plan.append(lambda ctx: ctx.emit(2, 3))
    x.set(1)
    x.set(2)  # sends nothing: a RESOLVED array does not replace the values
    assert sinks.on(n) == [((START,), (DATA, 2), (DATA, 3))]
    dep_runs: list[list[list[object]]] = []
    sinks.on(g.node([n], lambda ctx: dep_runs.append(ctx.deps[0].waves)))
    assert dep_runs == [[[2, 3]]]
    plan.append(lambda ctx: ctx.down([(INVALIDATE,)]))
    x.set(3)
    assert sinks.on(n) == [((START,),)]
    q = g.state(name="Q")

    def twice() -> None:
        q.set("q1")
        q.set("q2")

    g.batch(twice)
    assert sinks.on(q) == [((START,), (DATA, "q1"), (DATA, "q2"))]
    sinks.close()  # deactivate N and Q
    assert not n.has_value and q.has_value  # a computed node drops its values; a state keeps
    plan.append(lambda ctx: None)  # the reactivated N runs again and sends nothing
    assert sinks.on(n) == [((START,),)] and not plan  # no value kept from before
    assert sinks.on(q)[0] == ((START,), (DATA, "q1"), (DATA, "q2"))


# ---- C-44: only the settling RESOLVED, and a quiet node never holds a wave -----------------------


def test_c44_value_and_quiet_clauses() -> None:
    g = Graph("c44")
    a = g.state(0, name="A")
    f = g.node(
        [a],
        lambda ctx: [ctx.emit(v) for wave in ctx.deps[0].waves for v in wave if v % 2 == 0],
        name="F",
    )
    q = Kept(lambda ctx: ctx.emit("q") if ctx.deps[0].value == 2 else None)
    qn = g.node([a], q, name="Q", quiet=True)
    d_runs: list[tuple[object, object]] = []

    def d_fn(ctx: Ctx) -> None:
        d_runs.append((ctx.deps[0].value, ctx.deps[1].value))
        ctx.emit(d_runs[-1])

    d = g.node([a, qn], d_fn)
    f_seen, q_seen, d_seen = sink(f), sink(qn), sink(d)
    d_runs.clear()
    a.set(3)
    assert f_seen[-1] == ((PENDING,), (RESOLVED,)) and d_runs == [(3, SENTINEL)]
    a.set(2)  # Q sends during its run: a later wave of its own, which runs D again
    assert d_runs[-2:] == [(2, SENTINEL), (2, "q")]
    assert q.ctx is not None
    q.ctx.emit("later")
    assert d_runs[-1] == (2, "later")
    before = list(q_seen)
    a.set(5)
    assert q_seen == before  # Q sent nothing and no RESOLVED is added for it
    a.invalidate()
    assert q_seen[-1] == d_seen[-1] == ((PENDING,), (INVALIDATE,))


@pytest.mark.parametrize("policy", ["complete", "invalidate"])
def test_c44_a_wave_of_terminals_or_invalidates_holds_a_manual_fan_in(policy: str) -> None:
    g = Graph("c44-fan-in")
    s = g.state(1, name="S")
    b = g.node([s], echo, name="B")
    c = g.node([s], echo, name="C")
    runs: list[list[object]] = []

    def d(ctx: Ctx) -> None:
        runs.append([(dep.terminal, dep.invalidated) for dep in ctx.deps])

    options: dict[str, Any] = {policy: "manual"}
    dn = g.node([b, c], d, name="D", **options)
    b_seen = sink(b)
    dn.subscribe(lambda _: None)
    runs.clear()
    if policy == "complete":
        s.complete()
        assert b_seen[-1] == ((PENDING,), (COMPLETE,))
        assert runs == [[((COMPLETE,), False), ((COMPLETE,), False)]]
    else:
        s.invalidate()
        assert runs == [[(None, True), (None, True)]]


# ---- review Q15: a long chain through the facade ------------------------------------------------


def test_a_derived_chain_three_times_the_recursion_limit_runs_and_closes() -> None:
    import sys

    g = Graph("chain")
    head = g.state(0, name="n0")
    node: Node[Any] = head
    for i in range(1, 3 * sys.getrecursionlimit() + 1):
        node = g.derived([node], lambda v: v + 1, name=f"n{i}")
    seen = sink(node)
    head.set(1)
    assert seen[-1] == ((PENDING,), (DATA, 1 + 3 * sys.getrecursionlimit()))
    g.close()
    assert g.closed


# ---- C-48: the graph layer, find, describe, observe and retain (R-graph-surface) ----


def test_c48_find_describe_observe_and_retain() -> None:
    g = Graph("c48")
    a = g.state(1, name="a")
    b = g.derived([a], lambda v: v * 10, name="b")
    c = g.derived([a], lambda v: v, name="c")
    kept: list[Ctx] = []

    def keep(ctx: Ctx) -> None:
        kept.append(ctx)

    d = g.node([a], keep, name="d")

    # find gives b and nothing for an unknown id; neither find nor describe activates b.
    assert g.find("b") is b and g.find("nope") is None
    assert g.describe() == [
        {"id": "a", "kind": "state", "deps": [], "quiet": False},
        {"id": "b", "kind": "node", "deps": ["a"], "quiet": False},
        {"id": "c", "kind": "node", "deps": ["a"], "quiet": False},
        {"id": "d", "kind": "node", "deps": ["a"], "quiet": False},
    ]
    assert b.status.lifecycle == "inactive"

    # The first observer activates b: START with b's value, then [PENDING, DATA 20].
    first: list[GraphEvent] = []
    stop_first = g.observe([b], first.append)
    assert b.status.lifecycle == "active"
    a.set(2)
    assert [(e.id, e.msgs) for e in first] == [
        ("b", ((START,), (DATA, 10))),
        ("b", ((PENDING,), (DATA, 20))),
    ]
    assert {f.name for f in fields(GraphEvent)} == {"id", "msgs"}  # no tier, no seq

    # A second observer goes on after the first stops; after both stop, b is inactive.
    second: list[GraphEvent] = []
    stop_second = g.observe([b], second.append)
    stop_first.unsubscribe()
    a.set(3)
    assert len(first) == 2
    assert [(e.id, e.msgs) for e in second] == [
        ("b", ((START,), (DATA, 20))),
        ("b", ((PENDING,), (DATA, 30))),
    ]
    stop_second.unsubscribe()
    assert b.status.lifecycle == "inactive"

    # Each retain is its own subscription.
    one, two = g.retain(c), g.retain(c)
    assert c.status.lifecycle == "active"
    one.unsubscribe()
    assert c.status.lifecycle == "active"
    two.unsubscribe()
    assert c.status.lifecycle == "inactive"

    # describe gives d's current deps after a rewire; its definition keeps the declared.
    held = g.retain(d)
    kept[-1].rewire([a, c])
    assert [n["deps"] for n in g.describe() if n["id"] == "d"] == [["a", "c"]]
    assert g.definition(d).deps == (a,)
    held.unsubscribe()
    g.close()
