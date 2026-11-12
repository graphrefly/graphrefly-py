"""The public wire facades on the Python kernel (graphrefly-py:D8, graphrefly:D803).

C-1a (canonical protobuf bytes) against graphrefly spec/fixtures/protobuf, C-1c (the
envelope layer's explicit ack-timeout ingress), and C-1 and C-1b through the public
facades over protobuf bytes. The kernel-level C-1, C-1b and C-47 are in
tests/kernel/test_kernel_bridge.py.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import graphrefly
from graphrefly import (
    Graph,
    GraphCallbackError,
    Message,
    WireBridgeAckTimeout,
    WireBridgeIssue,
    WireBridgeStatus,
    WireEdgeCause,
    frame_sink,
    frame_source,
    wire_bridge,
    wire_bridge_ack_driver,
    wire_edge_group_in,
    wire_edge_group_out,
)
from graphrefly._wire import protobuf as pb

FIXTURES = Path(__file__).resolve().parents[2] / "graphrefly" / "spec" / "fixtures" / "protobuf"


def vectors(name: str) -> list[dict[str, Any]]:
    path = FIXTURES / name
    if not path.exists() and os.environ.get("CI"):
        pytest.fail(f"canonical protocol fixtures are required in CI: {path}")
    if not path.exists():
        pytest.skip(f"graphrefly fixtures not checked out next to this repository ({path})")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def data_of(arrays: list[tuple[Message[Any], ...]]) -> list[Any]:
    return [m[1] for a in arrays for m in a if m[0] == "DATA"]


def collect(node: graphrefly.Node[Any]) -> list[Any]:
    seen: list[Any] = []
    node.subscribe(lambda array: seen.extend(m[1] for m in array if m[0] == "DATA"))
    return seen


# ---- C-1a: canonical protobuf bytes ---------------------------------------------------


@pytest.mark.parametrize("name", ["wire_bridge_envelope.v1.jsonl", "wire_edge_cause.v1.jsonl"])
def test_c1a_golden_vectors_round_trip_and_negatives_fail_closed(name: str) -> None:
    decode: Callable[[bytes], Any] = pb.decode_envelope if "envelope" in name else pb.decode_cause
    encode: Callable[[Any], bytes] = pb.encode_envelope if "envelope" in name else pb.encode_cause
    checked = 0
    for vector in vectors(name):
        raw = bytes.fromhex(vector["hex"])
        if vector["canonical"]:
            value = decode(raw)
            assert encode(value) == raw, vector["id"]
            expect = vector.get("expect") or {}
            if "payload" in expect:
                kind = value.payload
                if kind == "data":
                    kind = "data.edge_cause" if value.edge_cause is not None else "data.value"
                assert kind == expect["payload"], vector["id"]
            if "edges" in expect:
                assert len(value.edges) == expect["edges"], vector["id"]
        else:
            with pytest.raises(pb.ProtobufError) as error:
                decode(raw)
            assert error.value.category == vector["errorCategory"], vector["id"]
        checked += 1
    assert checked >= 20


def test_c1a_malformed_inbound_bytes_are_bridge_issues_not_terminals() -> None:
    envelope_vectors = {
        v["id"]: bytes.fromhex(v["hex"]) for v in vectors("wire_bridge_envelope.v1.jsonl")
    }
    with Graph("c1a") as g:
        received = g.state(name="received")
        bridge = wire_bridge(g, session_id="s1", received=received, name="bridge")
        issues = collect(bridge.issues)
        frames = collect(bridge.frames)
        received.set(envelope_vectors["negative.unknown_field"])
        received.set(envelope_vectors["negative.edge_cause_kernel_only_message"])
        received.set(b"\xff\xff")
        received.set(envelope_vectors["positive.data.edge_cause"])
        assert [(i.code, i.category) for i in issues] == [
            ("invalid_bytes", "unknown_field"),
            ("invalid_bytes", "invalid_wire_edge"),
            ("invalid_bytes", "malformed"),
        ]
        assert len(frames) == 1 and isinstance(frames[0], WireEdgeCause)
        assert bridge.status.status.output == "open"


def test_c1a_outbound_bytes_are_canonical_envelopes() -> None:
    with Graph("c1a-out") as g:
        x = g.state(b"x0", name="x")
        out = wire_edge_group_out(g, group="g", edges={"x": x}, name="out")
        bridge = wire_bridge(g, session_id="s1", outbound=[out], name="bridge")
        sent = collect(bridge.outbound_bytes)
        x.set(b"x1")
        envelopes = [pb.decode_envelope(raw) for raw in sent]
        assert [pb.encode_envelope(e) for e in envelopes] == sent
        assert [e.metadata.seq for e in envelopes] == [1, 2]
        assert [e.edge_cause.cause for e in envelopes if e.edge_cause] == [1, 2]


def test_c1a_no_raw_envelope_construction_is_public() -> None:
    for name in (
        "Envelope",
        "Metadata",
        "WireBridgeEnvelope",
        "WireBridgeCommand",
        "encode_envelope",
    ):
        assert not hasattr(graphrefly, name)


# ---- C-1 and C-1b through the public facades over protobuf bytes -----------------------


class BytesLink:
    """Carries one bridge's outbound bytes to another graph's receiving end, by hand."""

    def __init__(self) -> None:
        self.queue: list[bytes] = []
        self.deliver: Callable[[object], None] | None = None

    def open(self, deliver: Callable[[object], None]) -> Callable[[], None]:
        self.deliver = deliver
        return lambda: None

    def flush(self) -> None:
        items = list(self.queue)
        self.queue.clear()  # in place: sinks hold this list's append
        assert self.deliver is not None
        for item in items:
            self.deliver(item)


def test_c1_a_cross_graph_diamond_through_bridges_and_bytes() -> None:
    g1, g2 = Graph("g1"), Graph("g2")
    to_g2, to_g1 = BytesLink(), BytesLink()
    leg_runs: list[str] = []

    def leg(name: str, suffix: bytes) -> Callable[[bytes], bytes]:
        def run(value: bytes) -> bytes:
            leg_runs.append(name)
            return value + suffix

        return run

    # g1 sends ga (edge a, from A) and receives gbc; g2 receives ga and sends gbc.
    a = g1.state(b"1", name="A")
    ga_out = wire_edge_group_out(g1, group="ga", edges={"a": a}, name="ga.out")
    b1 = wire_bridge(
        g1, session_id="g1", outbound=[ga_out], received=frame_source(g1, to_g1.open), name="b1"
    )
    b2_in = wire_bridge(g2, session_id="g2-in", received=frame_source(g2, to_g2.open), name="b2.in")
    ga_in = wire_edge_group_in(g2, group="ga", frames=b2_in.frames, edges=["a"], name="ga.in")
    bb = g2.derived([ga_in.inbound["a"]], leg("B", b"b"), name="B")
    cc = g2.derived([ga_in.inbound["a"]], leg("C", b"c"), name="C")
    gbc_out = wire_edge_group_out(g2, group="gbc", edges={"b": bb, "c": cc}, name="gbc.out")
    b2_out = wire_bridge(g2, session_id="g2-out", outbound=[gbc_out], name="b2.out")
    gbc_in = wire_edge_group_in(g1, group="gbc", frames=b1.frames, edges=["b", "c"], name="gbc.in")
    d_runs: list[tuple[object, object]] = []

    def join(vb: object, vc: object) -> tuple[object, object]:
        d_runs.append((vb, vc))
        return (vb, vc)

    d = g1.derived([gbc_in.inbound["b"], gbc_in.inbound["c"]], join, name="D")
    d_arrays: list[tuple[Message[Any], ...]] = []
    d.subscribe(d_arrays.append)
    b1_status: list[WireBridgeStatus] = collect(b1.status)
    b1.outbound_bytes.subscribe(frame_sink(to_g2.queue.append))
    b2_in.outbound_bytes.subscribe(frame_sink(to_g1.queue.append))  # acks back to g1
    b2_out.outbound_bytes.subscribe(frame_sink(to_g1.queue.append))

    to_g2.flush()  # ga's first cause reaches B and C
    to_g1.flush()  # the ack and gbc's first cause reach g1
    assert d_runs[-1] == (b"1b", b"1c")

    leg_runs.clear()
    d_runs.clear()
    d_arrays.clear()
    a.set(b"2")
    to_g2.flush()
    assert sorted(leg_runs) == ["B", "C"]  # one g2 wave, each leg once
    to_g1.flush()
    assert d_runs == [(b"2b", b"2c")]  # once, on both legs of the same A
    assert [data_of([array]) for array in d_arrays] == [[(b"2b", b"2c")]]
    assert b1_status[-1].pending == () and b1_status[-1].acked == 2  # g2 acked every envelope
    g1.close()
    g2.close()


def test_c1b_a_value_that_is_not_bytes_is_the_outbound_nodes_error() -> None:
    with Graph("not-bytes") as g:
        x: Any = g.state("text", name="x")
        out = wire_edge_group_out(g, group="g", edges={"x": x})
        seen: list[tuple[Message[Any], ...]] = []
        out.subscribe(seen.append)
        error = seen[0][-1]
        assert error[0] == "ERROR" and isinstance(error[1], GraphCallbackError)
        assert "not bytes" in error[1].message


def test_c1b_frames_by_hand_through_the_public_receiver() -> None:
    with Graph("receiver") as g:
        deliver: list[Callable[[object], None]] = []

        def open_(d: Callable[[object], None]) -> None:
            deliver.append(d)

        frames = frame_source(g, open_, name="frames")
        group = wire_edge_group_in(g, group="g", frames=frames, edges=["x"], name="in")
        issues = collect(group.issues)
        values = collect(group.inbound["x"])
        hand = deliver[-1]
        cause = WireEdgeCause(
            "g", "s", 1, (graphrefly.WireEdgeArrays("x", (((("DATA", b"v1"),),))),)
        )
        hand(cause)
        hand(cause)  # a duplicate: dropped
        assert values == [b"v1"]
        assert [i.kind for i in issues] == ["stale"]
        assert group.inbound["x"].status.output == "open"


# ---- C-1c: explicit ack-timeout ingress ------------------------------------------------------


def _sender(timeout_ms: int = 5) -> dict[str, Any]:
    g = Graph("c1c")
    source = g.state(b"first", name="edge-source")
    out = wire_edge_group_out(g, group="g", edges={"edge-a": source}, name="group")
    bridge = wire_bridge(g, session_id="s1", outbound=[out], name="bridge")
    clock = g.state(1000, name="clock")
    ack = wire_bridge_ack_driver(g, bridge, clock=clock, timeout_ms=timeout_ms, name="ack")
    return {"g": g, "bridge": bridge, "clock": clock, "ack": ack, "source": source}


def test_c1c_clock_driven_timeouts_retry_and_then_exhaust() -> None:
    t = _sender()
    bridge, clock, ack = t["bridge"], t["clock"], t["ack"]
    sent = collect(bridge.outbound_bytes)
    statuses: list[WireBridgeStatus] = collect(bridge.status)
    issues: list[WireBridgeIssue] = collect(bridge.issues)
    timeouts: list[WireBridgeAckTimeout] = collect(ack.timeouts)
    ack_status = collect(ack.status)
    assert len(sent) == 1
    clock.set(1004)
    assert timeouts == []
    clock.set(1005)
    assert timeouts[-1] == WireBridgeAckTimeout(1, 1, 1005)
    retry = pb.decode_envelope(sent[-1])
    assert (len(sent), retry.metadata.seq, retry.metadata.attempt) == (2, 1, 2)
    assert statuses[-1].state == "waiting" and statuses[-1].pending == ((1, 2),)
    clock.set(1010)
    assert timeouts[-1] == WireBridgeAckTimeout(1, 2, 1010)
    final = statuses[-1]
    assert final.state == "exhausted" and final.pending == ()
    assert issues[-1].code == "exhausted" and "ack timeout for seq 1" in issues[-1].message
    assert ack_status[-1].last_timeout == timeouts[-1]
    t["g"].close()


def test_c1c_stale_mismatched_and_malformed_ingress_fail_closed() -> None:
    t = _sender()
    bridge = t["bridge"]
    sent = collect(bridge.outbound_bytes)
    statuses: list[WireBridgeStatus] = collect(bridge.status)
    issues: list[WireBridgeIssue] = collect(bridge.issues)
    bridge.ack_timeouts.set(WireBridgeAckTimeout(1, 2, 1005))  # mismatched attempt: no-op
    bridge.ack_timeouts.set(WireBridgeAckTimeout(9, 1, 1005))  # unknown seq: no-op
    assert issues == [] and len(sent) == 1 and statuses[-1].pending == ((1, 1),)
    bridge.ack_timeouts.set(WireBridgeAckTimeout(1, 0, 1005))
    bridge.ack_timeouts.set(WireBridgeAckTimeout(0, 1, 1005))
    facts: Any = bridge.ack_timeouts
    facts.set("not a fact")
    assert [i.message for i in issues] == [
        "wireBridge: ack-timeout command attempt must be positive",
        "wireBridge: ack-timeout command seq must be positive",
        "wireBridge: ack-timeout facts are WireBridgeAckTimeout",
    ]
    assert bridge.status.status.output == "open"  # an issue is never a terminal
    assert not any(s.state == "exhausted" for s in statuses)
    t["g"].close()


def test_c1c_the_ack_path_clears_pending_on_the_sending_bridge() -> None:
    g = Graph("ack-path")
    source = g.state(b"v", name="source")
    received = g.state(name="received")
    out = wire_edge_group_out(g, group="g", edges={"x": source}, name="out")
    bridge = wire_bridge(g, session_id="s1", outbound=[out], received=received, name="bridge")
    peer_in = g.state(name="peer-in")
    peer = wire_bridge(g, session_id="peer", received=peer_in, name="peer")
    sent = collect(bridge.outbound_bytes)
    acks = collect(peer.outbound_bytes)
    statuses: list[WireBridgeStatus] = collect(bridge.status)
    peer_in.set(sent[0])
    received.set(acks[-1])
    assert statuses[-1].pending == () and statuses[-1].acked == 1
    bridge.ack_timeouts.set(WireBridgeAckTimeout(1, 1, 2000))  # stale after the ack: no-op
    assert len(sent) == 1
    g.close()


def test_c1c_release_stops_the_driver_and_the_bridge() -> None:
    t = _sender()
    bridge, clock, ack = t["bridge"], t["clock"], t["ack"]
    sent = collect(bridge.outbound_bytes)
    timeouts = collect(ack.timeouts)
    ack.release()
    ack.release()
    clock.set(1005)
    assert timeouts == [] and len(sent) == 1
    bridge.release()
    assert bridge.released and ack.released
    t["source"].set(b"after release")
    assert len(sent) == 1
    with pytest.raises(graphrefly.GraphReflyRuntimeError, match="released"):
        wire_bridge_ack_driver(t["g"], bridge, clock=clock, timeout_ms=5)
    t["g"].close()


def test_c1c_subscription_order_keeps_timeouts_and_issues_visible() -> None:
    t = _sender()
    bridge, clock, ack = t["bridge"], t["clock"], t["ack"]
    bridge.outbound_bytes.subscribe(lambda _: None)
    ack.status.subscribe(lambda _: None)
    clock.set(1005)
    late = collect(ack.timeouts)  # subscribed after the timeout: its snapshot carries it
    assert late == [WireBridgeAckTimeout(1, 1, 1005)]
    issues = collect(ack.issues)
    any_clock: Any = clock
    any_clock.set("bad")
    clock.set(1)
    assert [i.code for i in issues] == ["invalid_clock", "invalid_clock"]
    assert "non-negative integers" in issues[0].message and "monotonic" in issues[1].message
    with pytest.raises(graphrefly.GraphReflyValueError, match="timeout_ms"):
        wire_bridge_ack_driver(t["g"], bridge, clock=clock, timeout_ms=0)
    t["g"].close()


def test_c1c_no_hidden_timer_or_retry_pump_in_the_bridge() -> None:
    root = Path(graphrefly.__file__).resolve().parent
    sources = [root / "_wire_facade.py", *sorted((root / "_wire").glob("*.py"))]
    text = "\n".join(path.read_text() for path in sources)
    for word in (
        "threading.Timer",
        "call_later",
        "sleep(",
        "asyncio",
        "setTimeout",
        "time.time",
        "monotonic(",
    ):
        assert word not in text, word


def test_ci_refuses_missing_canonical_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(globals(), "FIXTURES", tmp_path)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="canonical protocol fixtures are required"):
        vectors("wire_bridge_envelope.v1.jsonl")


def test_bridge_retains_commands_and_event_indices_without_external_subscribers() -> None:
    with Graph() as g:
        source = g.state(name="source")
        bridge = wire_bridge(g, session_id="s", outbound=[source], name="bridge")
        for value in (b"a", b"b", b"c"):
            source.set(value)
        indexed = collect(bridge.events)
        assert len(indexed) == 1 and set(indexed[0]) == {"index", "event"}
        assert indexed[0]["index"] == 3
        assert indexed[0]["event"]["kind"] == "outbound"
        assert indexed[0]["event"]["envelope"].metadata.seq == 3
        statuses = collect(bridge.status)
        assert statuses[-1].next_seq == 4
        assert statuses[-1].pending == ((1, 1), (2, 1), (3, 1))
        source.set(b"d")
        assert indexed[-1]["index"] == 4
        assert indexed[-1]["event"]["envelope"].metadata.seq == 4
        bridge.release()
        source.set(b"ignored")
        bridge.ack_timeouts.set(WireBridgeAckTimeout(1, 1))
        assert indexed[-1]["index"] == 4


def test_bridge_status_ledger_skips_replayed_events_and_catches_up_after_inactive() -> None:
    with Graph() as g:
        source = g.state(name="source")
        received = g.state(name="received")
        bridge = wire_bridge(g, session_id="s", outbound=[source], received=received, name="b")
        statuses: list[WireBridgeStatus] = []
        stop = bridge.status.subscribe(lambda ms: statuses.extend(data_of([ms])))
        source.set(b"a")
        stop()
        source.set(b"b")
        for seq in (1, 2):
            received.set(
                pb.encode_envelope(
                    pb.Envelope(
                        "peer", pb.Metadata(seq, 0, f"peer:{seq}", 1, 2, ack_for_seq=seq), "ack"
                    )
                )
            )
        stop = bridge.status.subscribe(lambda ms: statuses.extend(data_of([ms])))
        assert statuses[-1].acked == 2 and statuses[-1].pending == ()
        stop()
        ledger = g.find("b.status_ledger")
        assert ledger is not None and ledger.cache()["highWater"] == 6
        stop = bridge.status.subscribe(lambda ms: statuses.extend(data_of([ms])))
        assert statuses[-1].acked == 2
        stop()
        assert ledger.cache()["highWater"] == 6
        assert "b.status_ledger" in next(n for n in g.describe() if n["id"] == "b.status")["deps"]


def test_bridge_release_drops_internal_retains_and_ack_driver_stops() -> None:
    t = _sender()
    g, bridge, ack, clock = t["g"], t["bridge"], t["ack"], t["clock"]
    # No external subscribers: the driver still sees the clock and emits a retry.
    clock.set(1005)
    events = bridge.events.cache()
    assert events["event"]["kind"] == "outbound"
    assert events["event"]["envelope"].metadata.attempt == 2
    bridge.release()
    bridge.release()
    for name in ("bridge", "bridge.events", "ack"):
        node = g.find(name)
        assert node is not None and node.status.lifecycle == "inactive"
    clock.set(1010)
    assert bridge.released and ack.released
    g.close()
