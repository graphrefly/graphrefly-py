import pytest

import graphrefly
from graphrefly import DataMessage, Graph, Message

TraceRow = dict[str, object]


def _record_data(target: list[bytes]):
    def record(message: Message[bytes]) -> None:
        if isinstance(message, DataMessage):
            target.append(message.value)

    return record


def _make_bridge(graph: Graph, *, session_id: str, name: str):
    bridge = graphrefly.wire_bridge(graph, session_id=session_id, name=f"{name}/bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name=f"{name}/protobuf")
    return bridge, protobuf


def _varint_at(data: bytes, index: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while index < len(data):
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, index
        shift += 7
    raise ValueError("truncated varint")


def _fields(data: bytes) -> list[tuple[int, int, bytes | int]]:
    out: list[tuple[int, int, bytes | int]] = []
    index = 0
    while index < len(data):
        key, index = _varint_at(data, index)
        field_no = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            value, index = _varint_at(data, index)
            out.append((field_no, wire_type, value))
            continue
        if wire_type == 2:
            length, index = _varint_at(data, index)
            end = index + length
            if end > len(data):
                raise ValueError("truncated length-delimited field")
            out.append((field_no, wire_type, data[index:end]))
            index = end
            continue
        raise ValueError(f"unsupported wire type {wire_type}")
    return out


def _bytes_field(fields: list[tuple[int, int, bytes | int]], field_no: int) -> bytes | None:
    for no, wire_type, value in fields:
        if no == field_no and wire_type == 2 and isinstance(value, bytes):
            return value
    return None


def _varint_field(fields: list[tuple[int, int, bytes | int]], field_no: int) -> int | None:
    for no, wire_type, value in fields:
        if no == field_no and wire_type == 0 and isinstance(value, int):
            return value
    return None


def _decode_pump_bytes(raw: bytes) -> TraceRow:
    row: TraceRow = {
        "bytes_len": len(raw),
        "session_id": None,
        "seq": None,
        "cursor": None,
        "attempt": None,
        "envelope_type": None,
        "payload_kind": None,
        "data_body_kind": None,
        "edge_id": None,
        "cause_id": None,
        "wire_edge_kind": None,
        "value_hex_prefix": None,
        "decode_error": None,
    }
    try:
        envelope_fields = _fields(raw)
        session = _bytes_field(envelope_fields, 1)
        if session is not None:
            row["session_id"] = session.decode()
        metadata = _bytes_field(envelope_fields, 2)
        if metadata is not None:
            metadata_fields = _fields(metadata)
            row["seq"] = _varint_field(metadata_fields, 1)
            row["cursor"] = _varint_field(metadata_fields, 2)
            row["attempt"] = _varint_field(metadata_fields, 4)
        payload_field = next(
            (
                (no, value)
                for no, wire_type, value in envelope_fields
                if 3 <= no <= 9 and wire_type == 2
            ),
            None,
        )
        if payload_field is None or not isinstance(payload_field[1], bytes):
            return row
        payload_no, payload = payload_field
        envelope_type = {
            3: "start",
            4: "data",
            5: "ack",
            6: "nack",
            7: "status",
            8: "error",
            9: "close",
        }.get(payload_no)
        row["envelope_type"] = envelope_type
        row["payload_kind"] = envelope_type
        if payload_no != 4:
            return row
        data_fields = _fields(payload)
        value = _bytes_field(data_fields, 1)
        frame = _bytes_field(data_fields, 2)
        if value is not None:
            row["data_body_kind"] = "value"
            row["value_hex_prefix"] = value[:16].hex()
            return row
        if frame is None:
            return row
        row["data_body_kind"] = "wire_edge"
        frame_fields = _fields(frame)
        kind = _varint_field(frame_fields, 1)
        edge_id = _bytes_field(frame_fields, 2)
        cause_id = _bytes_field(frame_fields, 3)
        edge_value = _bytes_field(frame_fields, 4)
        row["wire_edge_kind"] = {1: "dirty", 2: "data"}.get(kind, kind)
        row["edge_id"] = edge_id.decode() if edge_id is not None else None
        row["cause_id"] = cause_id.decode() if cause_id is not None else None
        row["value_hex_prefix"] = None if edge_value is None else edge_value[:16].hex()
    except Exception as error:  # noqa: BLE001 - diagnostic probe must not affect transport.
        row["decode_error"] = str(error)
    return row


def _trace_pump_row(
    trace: list[TraceRow] | None,
    *,
    phase: str,
    direction: str,
    pump_step: int,
    queue_index: int,
    raw: bytes,
) -> None:
    if trace is None:
        return
    trace.append(
        {
            "phase": phase,
            "direction": direction,
            "pump_step": pump_step,
            "queue_index": queue_index,
            **_decode_pump_bytes(raw),
        }
    )


def _pump(
    queue: list[bytes],
    target: graphrefly.WireBridgeProtobuf,
    *,
    trace: list[TraceRow] | None = None,
    phase: str = "unspecified",
    direction: str = "unspecified",
) -> int:
    pumped = 0
    while queue:
        raw = queue.pop(0)
        _trace_pump_row(
            trace,
            phase=phase,
            direction=direction,
            pump_step=pumped,
            queue_index=0,
            raw=raw,
        )
        target.inbound_bytes.set(raw)
        pumped += 1
    return pumped


def _classify_replay_source(trace: list[TraceRow]) -> str:
    old_outbound = [
        row
        for row in trace
        if row["phase"] == "stimulus" and row["direction"] == "g1_to_g2"
        and row["wire_edge_kind"] == "data"
        and row["value_hex_prefix"] in {"422d696e3a6130", "432d696e3a6130"}
    ]
    if old_outbound:
        return "outbound cause generation"
    old_inbound_release = [
        row
        for row in trace
        if row["phase"] == "stimulus" and row["direction"] == "g2_to_g1"
        and row["wire_edge_kind"] == "data"
        and row["value_hex_prefix"] is not None
        and "6130" in str(row["value_hex_prefix"])
    ]
    if old_inbound_release:
        return "inbound WireEdgeGroup gate"
    warmup_after_clear = [
        row
        for row in trace
        if row["phase"] == "stimulus" and row["direction"] in {"g1_to_g2", "g2_to_g1"}
        and row["cause_id"] is not None
        and str(row["cause_id"]).endswith(":1")
    ]
    if warmup_after_clear:
        return "warmup/activation drain boundary"
    return "no old-cause replay observed in pump trace"


@pytest.mark.xfail(
    reason=(
        "B98/C-1: executable preparatory harness shape only; D560 removed the "
        "g1->g2 outbound stale-snapshot source, but the return leg still "
        "classifies as inbound WireEdgeGroup gate and must not flip C-1"
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
    trace: list[TraceRow] = []

    outbound_g1_to_g2: list[bytes] = []
    outbound_g2_to_g1: list[bytes] = []
    pump_g1_to_g2 = g1_to_g2_protobuf.outbound_bytes.subscribe(
        _record_data(outbound_g1_to_g2),
    )
    pump_g2_to_g1 = g2_to_g1_protobuf.outbound_bytes.subscribe(
        _record_data(outbound_g2_to_g1),
    )

    try:
        _pump(
            outbound_g1_to_g2,
            g2_from_g1_protobuf,
            trace=trace,
            phase="warmup",
            direction="g1_to_g2",
        )
        _pump(
            outbound_g2_to_g1,
            g1_from_g2_protobuf,
            trace=trace,
            phase="warmup",
            direction="g2_to_g1",
        )
        joined.clear()
        runs.clear()

        source.set(b"a1")
        assert (
            _pump(
                outbound_g1_to_g2,
                g2_from_g1_protobuf,
                trace=trace,
                phase="stimulus",
                direction="g1_to_g2",
            )
            > 0
        )
        assert (
            _pump(
                outbound_g2_to_g1,
                g1_from_g2_protobuf,
                trace=trace,
                phase="stimulus",
                direction="g2_to_g1",
            )
            > 0
        )

        classification = _classify_replay_source(trace)
        assert joined == [b"B-out:B-in:a1|C-out:C-in:a1"], (
            f"C-1 replay source classification: {classification}; trace={trace}"
        )
        assert runs == [(b"B-out:B-in:a1", b"C-out:C-in:a1")], (
            f"C-1 replay source classification: {classification}; trace={trace}"
        )
    finally:
        pump_g2_to_g1.unsubscribe()
        pump_g1_to_g2.unsubscribe()
        join_subscription.unsubscribe()
