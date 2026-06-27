import pytest

import graphrefly
from graphrefly import DataMessage, Graph, Message


def _record_data(target: list[bytes]):
    def record(message: Message[bytes]) -> None:
        if isinstance(message, DataMessage):
            target.append(message.value)

    return record


def _make_bridge(graph: Graph, *, session_id: str, name: str):
    bridge = graphrefly.wire_bridge(graph, session_id=session_id, name=f"{name}/bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name=f"{name}/protobuf")
    return bridge, protobuf


def _pump(queue: list[bytes], target: graphrefly.WireBridgeProtobuf) -> int:
    pumped = 0
    while queue:
        target.inbound_bytes.set(queue.pop(0))
        pumped += 1
    return pumped


@pytest.mark.xfail(
    reason=(
        "B98/C-1: executable preparatory harness shape only; current high-level "
        "byte pump still replays duplicate old-cause DATA and must not flip C-1"
    ),
    strict=True,
)
def test_c1_preparatory_two_graph_bridge_diamond_harness_shape():
    g1 = Graph("py-c1-harness-g1")
    g2 = Graph("py-c1-harness-g2")

    g1_to_g2_bridge, g1_to_g2_protobuf = _make_bridge(
        g1,
        session_id="g1-g2",
        name="g1_to_g2",
    )
    _g2_from_g1_bridge, g2_from_g1_protobuf = _make_bridge(
        g2,
        session_id="g1-g2",
        name="g2_from_g1",
    )
    g2_to_g1_bridge, g2_to_g1_protobuf = _make_bridge(
        g2,
        session_id="g2-g1",
        name="g2_to_g1",
    )
    _g1_from_g2_bridge, g1_from_g2_protobuf = _make_bridge(
        g1,
        session_id="g2-g1",
        name="g1_from_g2",
    )

    source = g1.state(b"a0", name="A")
    a_to_b = g1.derived([source], lambda value: b"B-in:" + value, name="A_to_B_payload")
    a_to_c = g1.derived([source], lambda value: b"C-in:" + value, name="A_to_C_payload")
    graphrefly.wire_edge_group(
        g1,
        g1_to_g2_bridge,
        outbound_edges={"a-to-b": a_to_b, "a-to-c": a_to_c},
        name="g1_split",
    )

    g2_in = graphrefly.wire_edge_group(
        g2,
        _g2_from_g1_bridge,
        inbound_edges=["a-to-b", "a-to-c"],
        name="g2_split_in",
    )
    b_leg = g2.derived(
        [g2_in.inbound_edges["a-to-b"]],
        lambda value: b"B-out:" + value,
        name="B",
    )
    c_leg = g2.derived(
        [g2_in.inbound_edges["a-to-c"]],
        lambda value: b"C-out:" + value,
        name="C",
    )
    graphrefly.wire_edge_group(
        g2,
        g2_to_g1_bridge,
        outbound_edges={"b-to-d": b_leg, "c-to-d": c_leg},
        name="g2_join_out",
    )

    g1_in = graphrefly.wire_edge_group(
        g1,
        _g1_from_g2_bridge,
        inbound_edges=["b-to-d", "c-to-d"],
        name="g1_join_in",
    )
    runs: list[tuple[bytes, bytes]] = []
    join = g1.derived(
        [g1_in.inbound_edges["b-to-d"], g1_in.inbound_edges["c-to-d"]],
        lambda b_value, c_value: runs.append((b_value, c_value))
        or b_value + b"|" + c_value,
        name="D",
    )
    joined: list[bytes] = []
    join_subscription = join.subscribe(_record_data(joined))

    outbound_g1_to_g2: list[bytes] = []
    outbound_g2_to_g1: list[bytes] = []
    pump_g1_to_g2 = g1_to_g2_protobuf.outbound_bytes.subscribe(
        _record_data(outbound_g1_to_g2),
    )
    pump_g2_to_g1 = g2_to_g1_protobuf.outbound_bytes.subscribe(
        _record_data(outbound_g2_to_g1),
    )

    try:
        _pump(outbound_g1_to_g2, g2_from_g1_protobuf)
        _pump(outbound_g2_to_g1, g1_from_g2_protobuf)
        joined.clear()
        runs.clear()

        source.set(b"a1")
        assert _pump(outbound_g1_to_g2, g2_from_g1_protobuf) > 0
        assert _pump(outbound_g2_to_g1, g1_from_g2_protobuf) > 0

        assert joined == [b"B-out:B-in:a1|C-out:C-in:a1"]
        assert runs == [(b"B-out:B-in:a1", b"C-out:C-in:a1")]
    finally:
        pump_g2_to_g1.unsubscribe()
        pump_g1_to_g2.unsubscribe()
        join_subscription.unsubscribe()
