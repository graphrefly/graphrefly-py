"""Phase 6: graphs on one thread, and the wire bridge module (R-graph-domain@3, R-bridge@2).

Scenarios C-47, C-1 and C-1b of graphrefly spec/conformance.jsonl, qualified against
graphrefly._kernel and graphrefly._wire.bridge (graphrefly:D803; R-bridge@2 by
graphrefly:D804). The tests carry frames by hand.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from graphrefly._checkpoint import CheckpointError, capture
from graphrefly._kernel import COMPLETE, DATA, ERROR, INVALIDATE, PENDING, Ctx, Graph, Message
from graphrefly._wire.bridge import (
    WireEdgeGroupIssue,
    frame_sink,
    frame_source,
    wire_edge_group_in,
    wire_edge_group_out,
)
from graphrefly._wire.protobuf import WireEdgeArrays, WireEdgeCause


class Channel:
    """A hand-carried transport: frames sent pile up until delivered."""

    def __init__(self) -> None:
        self.sent: list[WireEdgeCause] = []
        self.deliver: Callable[[object], None] | None = None
        self.opened = 0

    def send(self, frame: object) -> None:
        assert isinstance(frame, WireEdgeCause)
        self.sent.append(frame)

    def open(self, deliver: Callable[[object], None]) -> Callable[[], None]:
        self.deliver = deliver
        self.opened += 1

        def close() -> None:
            self.deliver = None

        return close

    def flush(self) -> None:
        frames, self.sent = self.sent, []
        for frame in frames:
            self.hand(frame)

    def hand(self, frame: object) -> None:
        assert self.deliver is not None, "the receiving end is not open"
        self.deliver(frame)


# ---- C-47: graphs on one thread share no wave, request queue or batch -------------


def test_c47_graphs_on_one_thread_share_no_wave_queue_or_batch() -> None:
    g1, g2 = Graph(), Graph()
    g1.state("a", 0)
    g1.state("c", 0)
    g2.state("b", 0)
    log: list[str] = []

    def s1(array: tuple[Message, ...]) -> None:
        if array == ((PENDING,), (DATA, 1)):
            g2.set("b", 10)  # not held by g1's wave
            log.append(f"s1 after b: b={g2.get('b').value}")
            with pytest.raises(CheckpointError, match="wave boundary"):
                capture(g1)  # g1 is in its wave
            log.append(f"g2 capture: {len(capture(g2)['nodes'])} node")

    def s2(array: tuple[Message, ...]) -> None:
        if array[0] == (PENDING,):
            log.append(f"s2 sees b={array[1][1]}")
            g1.set("c", 5)  # waits for g1's wave to end

    def sc(array: tuple[Message, ...]) -> None:
        if array[0] == (PENDING,):
            log.append(f"c={array[1][1]}")

    g1.subscribe("s1", "a", s1)
    g1.subscribe("sc", "c", sc)
    g2.subscribe("s2", "b", s2)
    g1.set("a", 1)
    assert log == ["s2 sees b=10", "s1 after b: b=10", "g2 capture: 1 node", "c=5"]

    reads: list[object] = []

    def body() -> None:
        g1.set("a", 2)
        g2.set("b", 20)  # g2's own request: it runs at once
        reads.append(g2.get("b").value)
        reads.append(g1.get("a").value)  # a commits only when the body returns

    g1.batch(body)
    assert reads == [20, 1]
    assert g1.get("a").value == 2


# ---- C-1b: a group releases each cause whole, in order, at most once per session ----


def _groups() -> dict[str, Any]:
    sender, receiver = Graph(), Graph()
    sender.state("xin", b"x0")
    sender.state("y", b"y0")

    def x(ctx: Ctx) -> None:  # sends two arrays in one wave for the value b"two"
        dep = ctx.deps[0]
        if dep.invalidated:
            ctx.down([(INVALIDATE,)])
        for wave in dep.waves:
            for value in wave:
                if value == b"two":
                    ctx.down([(DATA, b"t1")])
                    ctx.down([(DATA, b"t2")])
                else:
                    ctx.down([(DATA, value)])

    sender.node("x", ["xin"], x, invalidate="manual")
    wire_edge_group_out(sender, "out", group="g", edges={"x": "x", "y": "y"})
    channel = Channel()
    frame_source(receiver, "frames", channel.open)
    group = wire_edge_group_in(receiver, "in", group="g", frames="frames", edges=["x", "y"])
    runs: list[list[list[list[object]]]] = []
    terminals: list[list[object]] = []

    def f(ctx: Ctx) -> None:
        runs.append([d.waves for d in ctx.deps])
        terminals.append([d.terminal for d in ctx.deps])

    receiver.node("F", [group.inbound["x"], group.inbound["y"]], f, complete="manual")
    issues: list[WireEdgeGroupIssue] = []
    receiver.subscribe(
        "issues",
        group.issues,
        lambda array: issues.extend(m[1] for m in array if m[0] == DATA),
    )
    inbound_x: list[tuple[Message, ...]] = []
    receiver.subscribe("ix", group.inbound["x"], inbound_x.append)
    receiver.subscribe("f", "F")
    sender.subscribe("transport", "out", frame_sink(channel.send))
    return {
        "sender": sender,
        "receiver": receiver,
        "channel": channel,
        "group": group,
        "runs": runs,
        "terminals": terminals,
        "issues": issues,
        "inbound_x": inbound_x,
    }


def test_c1b_each_sender_wave_is_one_cause_and_one_receiver_wave() -> None:
    t = _groups()
    sender, channel, runs = t["sender"], t["channel"], t["runs"]
    (first,) = channel.sent
    assert (first.group, first.cause) == ("g", 1)
    assert [(e.edge_id, e.arrays) for e in first.edges] == [
        ("x", (((DATA, b"x0"),),)),
        ("y", (((DATA, b"y0"),),)),
    ]
    channel.flush()
    assert runs[-1] == [[[b"x0"]], [[b"y0"]]]

    runs.clear()
    sender.set("xin", b"x1")
    (only_x,) = channel.sent
    assert [e.edge_id for e in only_x.edges] == ["x"]
    channel.flush()
    # y sent nothing (the kernel settled it with RESOLVED, an empty array); F ran once.
    assert runs == [[[[b"x1"]], [[]]]]

    runs.clear()
    sender.set("xin", b"x2")
    sender.set("y", b"y1")
    assert [[e.edge_id for e in f.edges] for f in channel.sent] == [["x"], ["y"]]
    channel.flush()
    assert len(runs) == 2

    runs.clear()

    def both() -> None:
        sender.set("xin", b"x3")
        sender.set("y", b"y2")

    sender.batch(both)
    (together,) = channel.sent
    assert [e.edge_id for e in together.edges] == ["x", "y"]
    channel.flush()
    assert runs == [[[[b"x3"]], [[b"y2"]]]]

    runs.clear()
    sender.set("xin", b"two")
    (pair,) = channel.sent
    assert pair.edges[0].arrays == (((DATA, b"t1"),), ((DATA, b"t2"),))  # two arrays stay two
    channel.flush()
    assert runs == [[[[b"t1"], [b"t2"]], [[]]]]
    assert t["inbound_x"][-2:] == [((PENDING,), (DATA, b"t1")), ((PENDING,), (DATA, b"t2"))]


def test_c1b_invalidate_and_complete_arrive_as_the_inbound_nodes_own() -> None:
    t = _groups()
    sender, receiver, channel, group = t["sender"], t["receiver"], t["channel"], t["group"]
    channel.flush()
    sender.invalidate("xin")
    sender.complete("y")
    channel.flush()
    assert ((PENDING,), (INVALIDATE,)) in t["inbound_x"]
    assert receiver.get(group.inbound["y"]).ended == (COMPLETE,)
    assert t["issues"] == []


def test_c1b_a_new_session_starts_the_count_again_and_invalidates_an_edge_without_a_value() -> None:
    t = _groups()
    sender, channel = t["sender"], t["channel"]
    channel.flush()
    old_session = sender.get("out").values[0].session
    sender.unsubscribe("transport", "out")  # ends the outbound node's lifetime: the session
    sender.invalidate("xin")
    sender.subscribe("transport", "out", frame_sink(channel.send))
    (first,) = channel.sent
    assert first.cause == 1 and first.session != old_session
    assert [(e.edge_id, e.arrays[0][0][0]) for e in first.edges] == [("x", INVALIDATE), ("y", DATA)]
    channel.flush()
    assert t["inbound_x"][-1] == ((PENDING,), (INVALIDATE,))  # the old session's x is dropped
    assert t["issues"] == []  # a new session is neither a gap nor a stale cause


def test_c1b_the_gate_drops_reports_and_refuses_without_ending_an_inbound_node() -> None:
    t = _groups()
    sender, receiver, channel, group, issues = (
        t["sender"],
        t["receiver"],
        t["channel"],
        t["group"],
        t["issues"],
    )
    channel.flush()
    sender.set("xin", b"x1")
    (released,) = channel.sent
    channel.flush()
    runs = t["runs"]
    runs.clear()

    channel.hand(released)  # a duplicate of a released cause
    channel.hand(replace(released, cause=1))  # an older cause
    assert [i.kind for i in issues] == ["stale", "stale"]
    channel.hand(replace(released, cause=4))  # two past the last: released, gap reported
    gap = WireEdgeGroupIssue("gap", released.session, 4, missing_from=3, missing_to=3)
    assert issues[-1] == gap

    def carried() -> int:  # F's runs on data an edge brought
        return sum(1 for run in runs if any(wave for dep in run for wave in dep))

    assert carried() == 1
    bad = [
        replace(released, cause=5, edges=(WireEdgeArrays("z", (((DATA, b"z"),),)),)),
        replace(released, cause=6, edges=(WireEdgeArrays("x", (((PENDING,), (DATA, b"p")),)),)),
        replace(released, cause=7, edges=(WireEdgeArrays("x", (((DATA, b"r"), ("RESOLVED",)),)),)),
        replace(
            released,
            cause=8,
            edges=(WireEdgeArrays("x", (((COMPLETE,),), ((DATA, b"late"),))),),
        ),
    ]
    for frame in bad:
        channel.hand(frame)
    assert [i.kind for i in issues[-4:]] == ["refused"] * 4
    assert [i.cause for i in issues[-4:]] == [5, 6, 7, 8]
    channel.hand(replace(released, group="other", cause=99))  # another group's: ignored
    channel.hand(replace(released, cause=9))  # the next number: refused ones were taken
    assert issues[-1].kind == "refused"  # no gap reported
    assert len(issues) == 7
    for inbound in group.inbound.values():
        assert receiver.get(inbound).ended is None
    assert carried() == 2  # the gap release and cause 9; the refused frames released nothing

    seen: list[tuple[Message, ...]] = []
    receiver.subscribe("late-y", group.inbound["y"], seen.append)
    assert seen == [(("START",), (DATA, b"y0"))]  # y's last released value


def test_c1b_a_value_that_is_not_bytes_is_the_outbound_nodes_error() -> None:
    sender = Graph()
    sender.state("x", "text")
    wire_edge_group_out(sender, "out", group="g", edges={"x": "x"})
    sent: list[tuple[Message, ...]] = []
    sender.subscribe("s", "out", sent.append)
    (start, error) = sent[0]
    assert error[0] == ERROR and isinstance(error[1], TypeError)
    assert "not bytes" in str(error[1])


# ---- C-1: a cross-graph diamond runs its fan-in once per sender wave -----------------


def test_c1_a_cross_graph_diamond_runs_its_fan_in_once_per_sender_wave() -> None:
    g1, g2 = Graph(), Graph()
    to_g2, to_g1 = Channel(), Channel()
    g1.state("A", b"1")
    wire_edge_group_out(g1, "ga.out", group="ga", edges={"a": "A"})
    frame_source(g2, "ga.frames", to_g2.open)
    ga = wire_edge_group_in(g2, "ga.in", group="ga", frames="ga.frames", edges=["a"])

    def leg(suffix: bytes) -> Callable[[Ctx], None]:
        def run(ctx: Ctx) -> None:
            for wave in ctx.deps[0].waves:
                for value in wave:
                    assert isinstance(value, bytes)
                    ctx.down([(DATA, value + suffix)])

        return run

    leg_runs: list[str] = []

    def counted(name: str, fn: Callable[[Ctx], None]) -> Callable[[Ctx], None]:
        def run(ctx: Ctx) -> None:
            leg_runs.append(name)
            fn(ctx)

        return run

    g2.node("B", [ga.inbound["a"]], counted("B", leg(b"b")))
    g2.node("C", [ga.inbound["a"]], counted("C", leg(b"c")))
    wire_edge_group_out(g2, "gbc.out", group="gbc", edges={"b": "B", "c": "C"})
    frame_source(g1, "gbc.frames", to_g1.open)
    gbc = wire_edge_group_in(g1, "gbc.in", group="gbc", frames="gbc.frames", edges=["b", "c"])
    d_runs: list[tuple[object, object]] = []

    def d(ctx: Ctx) -> None:
        pair = (ctx.deps[0].value, ctx.deps[1].value)
        d_runs.append(pair)
        ctx.down([(DATA, pair)])

    g1.node("D", [gbc.inbound["b"], gbc.inbound["c"]], d)
    d_sink: list[tuple[Message, ...]] = []
    g1.subscribe("d", "D", d_sink.append)  # opens the gbc receiving end
    g1.subscribe("ga-transport", "ga.out", frame_sink(to_g2.send))
    g2.subscribe("gbc-transport", "gbc.out", frame_sink(to_g1.send))

    to_g2.flush()  # ga's first cause: B and C start on A
    to_g1.flush()  # gbc's first cause: D starts on B's and C's values over A
    assert d_runs[-1] == (b"1b", b"1c")

    leg_runs.clear()
    d_runs.clear()
    d_sink.clear()
    g1.set("A", b"2")
    (ga_cause,) = to_g2.sent
    assert [e.edge_id for e in ga_cause.edges] == ["a"]
    to_g2.flush()
    assert sorted(leg_runs) == ["B", "C"]  # each ran once in one g2 wave
    (gbc_cause,) = to_g1.sent
    assert [e.edge_id for e in gbc_cause.edges] == ["b", "c"]
    to_g1.flush()
    assert d_runs == [(b"2b", b"2c")]  # once, never one new and one old leg
    assert d_sink == [((PENDING,), (DATA, (b"2b", b"2c")))]

    d_runs.clear()
    g1.set("A", b"3")
    to_g2.flush()
    g1.set("A", b"4")
    to_g2.flush()
    assert len(to_g1.sent) == 2  # gbc's frames held back
    to_g1.flush()  # delivered as two g1 waves, in order
    assert d_runs == [(b"3b", b"3c"), (b"4b", b"4c")]


def test_c1b_a_run_in_which_no_edge_brought_input_forms_no_cause() -> None:
    sender = Graph()
    sender.state("raw", b"keep")

    def only_keep(ctx: Ctx) -> None:  # sends nothing for b"drop": RESOLVED reaches the group
        for wave in ctx.deps[0].waves:
            for value in wave:
                if value != b"drop":
                    ctx.down([(DATA, value)])

    sender.node("x", ["raw"], only_keep)
    wire_edge_group_out(sender, "out", group="g", edges={"x": "x"})
    channel = Channel()
    sender.subscribe("t", "out", frame_sink(channel.send))
    assert len(channel.sent) == 1
    sender.set("raw", b"drop")
    assert len(channel.sent) == 1  # no frame for a wave in which no edge sent
    sender.set("raw", b"next")
    assert [f.cause for f in channel.sent] == [1, 2]


def test_c1b_a_new_session_after_an_edge_ended_still_invalidates_the_other_edge() -> None:
    t = _groups()
    sender, receiver, channel, group = t["sender"], t["receiver"], t["channel"], t["group"]
    channel.flush()
    sender.complete("y")
    channel.flush()
    assert receiver.get(group.inbound["y"]).ended == (COMPLETE,)
    sender.unsubscribe("transport", "out")
    sender.invalidate("xin")  # x has no value when the new session opens
    sender.subscribe("transport", "out", frame_sink(channel.send))
    channel.flush()
    assert receiver.get(group.inbound["x"]).has is False  # the old session's x is dropped
    assert t["issues"] == []


def test_c1b_a_first_cause_invalidates_an_edge_whose_snapshot_is_its_terminal_alone() -> None:
    t = _groups()
    sender, receiver, channel, group = t["sender"], t["receiver"], t["channel"], t["group"]
    channel.flush()
    assert receiver.get(group.inbound["x"]).value == b"x0"
    sender.unsubscribe("transport", "out")
    sender.invalidate("xin")
    sender.complete("xin")  # x loses its value and completes while no session is open
    sender.subscribe("transport", "out", frame_sink(channel.send))
    (first,) = channel.sent
    assert dict((e.edge_id, e.arrays) for e in first.edges)["x"] == (
        ((INVALIDATE,),),
        ((COMPLETE,),),
    )
    channel.flush()
    inbound_x = receiver.get(group.inbound["x"])
    assert inbound_x.has is False and inbound_x.ended == (COMPLETE,)  # x0 does not survive
    assert t["issues"] == []


def test_c1b_arrays_for_an_ended_edge_are_skipped_and_the_rest_of_the_frame_released() -> None:
    t = _groups()
    sender, receiver, channel, group, issues = (
        t["sender"],
        t["receiver"],
        t["channel"],
        t["group"],
        t["issues"],
    )
    channel.flush()
    sender.complete("y")
    (ended,) = channel.sent
    channel.flush()
    y = group.inbound["y"]
    assert receiver.get(y).ended == (COMPLETE,)
    diverging = replace(
        ended,
        cause=ended.cause + 1,
        edges=(
            WireEdgeArrays("x", (((DATA, b"x9"),),)),
            WireEdgeArrays("y", (((DATA, b"y9"),),)),  # the sender's y goes on
        ),
    )
    channel.hand(diverging)
    assert receiver.get(group.inbound["x"]).value == b"x9"  # the rest of the frame released
    assert receiver.get(y).value == b"y0" and receiver.get(y).ended == (COMPLETE,)
    assert issues == [WireEdgeGroupIssue("ended", ended.session, ended.cause + 1, edge="y")]

    repeated = replace(
        ended,
        cause=ended.cause + 2,
        edges=(WireEdgeArrays("y", (((DATA, b"y0"),), ((COMPLETE,),))),),
    )
    channel.hand(repeated)  # skipped arrays that end with a terminal are no divergence
    assert len(issues) == 1

    refused = replace(
        ended,
        cause=ended.cause + 3,
        edges=(WireEdgeArrays("y", (((COMPLETE,),), ((DATA, b"late"),))),),
    )
    channel.hand(refused)  # an array after its terminal in one frame is still refused whole
    assert issues[-1].kind == "refused"
