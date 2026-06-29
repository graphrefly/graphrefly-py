import json
from collections.abc import Callable
from pathlib import Path

import pytest

import graphrefly
import graphrefly._native as native

AUTHORITY_ROOT = Path(__file__).resolve().parents[2] / "graphrefly"
FIXTURE_DIR = AUTHORITY_ROOT / "spec" / "fixtures" / "protobuf"


def _load_vectors(path: Path, message: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        record = json.loads(line)
        assert record["schema"] == "graphrefly.protobuf.golden.v1", line_no
        assert record["message"] == message, line_no
        assert record["id"].startswith(("positive.", "negative.")), line_no
        assert isinstance(record["description"], str) and record["description"], line_no
        assert isinstance(record["hex"], str) and len(record["hex"]) % 2 == 0, line_no
        assert all(ch in "0123456789abcdef" for ch in record["hex"]), line_no
        if record["canonical"]:
            assert "errorCategory" not in record, line_no
        else:
            assert record["errorCategory"], line_no
        records.append(record)
    return records


@pytest.mark.parametrize(
    ("path", "message", "validator", "roundtripper", "empty_value_ids"),
    [
        (
            FIXTURE_DIR / "wire_bridge_envelope.v1.jsonl",
            "WireBridgeEnvelope",
            native._validate_canonical_wire_bridge_envelope,
            native._roundtrip_canonical_wire_bridge_envelope,
            {"positive.data.empty_value"},
        ),
        (
            FIXTURE_DIR / "wire_edge_frame.v1.jsonl",
            "WireEdgeFrame",
            native._validate_canonical_wire_edge_frame,
            native._roundtrip_canonical_wire_edge_frame,
            {"positive.wire_edge.data_empty_value"},
        ),
    ],
)
def test_d497_canonical_protobuf_vectors(
    path: Path,
    message: str,
    validator: Callable[[bytes], object],
    roundtripper: Callable[[bytes], object],
    empty_value_ids: set[str],
) -> None:
    records = _load_vectors(path, message)
    assert any(record["canonical"] for record in records)
    assert any(not record["canonical"] for record in records)

    for record in records:
        fixture_bytes = bytes.fromhex(record["hex"])
        result = validator(fixture_bytes)
        roundtrip = roundtripper(fixture_bytes)
        if record["canonical"]:
            assert result.ok is True
            assert result.category is None
            assert result.message is None
            assert roundtrip.ok is True
            assert roundtrip.bytes == fixture_bytes
            assert roundtrip.category is None
            assert roundtrip.message is None
            if record["id"] in empty_value_ids:
                assert record["description"]
        else:
            assert result.ok is False
            assert result.category == record["errorCategory"]
            assert result.message
            assert roundtrip.ok is False
            assert roundtrip.bytes is None
            assert roundtrip.category == record["errorCategory"]
            assert roundtrip.message


def test_d523_public_wire_bridge_protobuf_facade_reports_malformed_bytes_as_status_issues():
    bridge_records = _load_vectors(
        FIXTURE_DIR / "wire_bridge_envelope.v1.jsonl",
        "WireBridgeEnvelope",
    )
    positive = next(record for record in bridge_records if record["id"] == "positive.data.value")
    negative = next(record for record in bridge_records if record["id"] == "negative.unknown_field")
    graph = graphrefly.Graph("py-c1a-public")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    statuses: list[graphrefly.WireBridgeProtobufStatus] = []
    issues: list[graphrefly.WireBridgeProtobufIssue] = []
    bridge_issues: list[graphrefly.WireBridgeIssue] = []

    def record_status(msg: graphrefly.Message[object]) -> None:
        if isinstance(msg, graphrefly.DataMessage):
            statuses.append(msg.value)

    def record_issue(msg: graphrefly.Message[object]) -> None:
        if isinstance(msg, graphrefly.DataMessage):
            issues.append(msg.value)

    with (
        protobuf.status.subscribe(record_status),
        protobuf.issues.subscribe(record_issue),
        bridge.issues.subscribe(
            lambda msg: bridge_issues.append(msg.value)
            if isinstance(msg, graphrefly.DataMessage)
            else None
        ),
    ):
        protobuf.inbound_bytes.set(bytes.fromhex(str(positive["hex"])))
        protobuf.inbound_bytes.set(bytes.fromhex(str(negative["hex"])))

    assert graphrefly.WireBridgeProtobufStatus("inbound", "valid") in statuses
    assert statuses[-1] == graphrefly.WireBridgeProtobufStatus("inbound", "invalid")
    assert issues[-1].category == "unknown_field"
    assert "WireBridgeEnvelope" in issues[-1].message
    assert bridge_issues[-1].code == "bridge_error"
    assert bridge_issues[-1].message.startswith("unknown_field:")
    assert "WireBridgeEnvelope" in bridge_issues[-1].message


def test_d542_protobuf_inbound_bytes_privately_drive_wire_edge_group_ingress():
    bridge_records = _load_vectors(
        FIXTURE_DIR / "wire_bridge_envelope.v1.jsonl",
        "WireBridgeEnvelope",
    )
    dirty = next(
        record for record in bridge_records if record["id"] == "positive.data.wire_edge_dirty"
    )
    data = next(
        record for record in bridge_records if record["id"] == "positive.data.wire_edge_data"
    )
    graph = graphrefly.Graph("py-c1a-inbound-attachment")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    group = graphrefly.wire_edge_group(
        graph,
        bridge,
        name="group",
        inbound_edges=["edge-a"],
    )
    released: list[bytes] = []
    statuses: list[graphrefly.WireBridgeProtobufStatus] = []
    issues: list[graphrefly.WireBridgeProtobufIssue] = []

    with (
        group.inbound_edges["edge-a"].subscribe(
            lambda msg: released.append(msg.value)
            if isinstance(msg, graphrefly.DataMessage)
            else None
        ),
        protobuf.status.subscribe(
            lambda msg: statuses.append(msg.value)
            if isinstance(msg, graphrefly.DataMessage)
            else None
        ),
        protobuf.issues.subscribe(
            lambda msg: issues.append(msg.value)
            if isinstance(msg, graphrefly.DataMessage)
            else None
        ),
    ):
        protobuf.inbound_bytes.set(bytes.fromhex(str(dirty["hex"])))
        protobuf.inbound_bytes.set(bytes.fromhex(str(data["hex"])))

    assert released == [b'{"ok":true}']
    assert statuses[-1] == graphrefly.WireBridgeProtobufStatus("inbound", "valid")
    assert issues == []


def test_d542_protobuf_outbound_bytes_are_canonical_byte_values():
    graph = graphrefly.Graph("py-c1a-outbound-bytes")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    source = graph.state(b'{"n":1}', name="edge-a")
    graphrefly.wire_edge_group(
        graph,
        bridge,
        name="group",
        outbound_edges={"edge-a": source},
    )
    outbound: list[bytes] = []

    with protobuf.outbound_bytes.subscribe(
        lambda msg: outbound.append(msg.value)
        if isinstance(msg, graphrefly.DataMessage)
        else None
    ):
        outbound.clear()
        source.set(b'{"n":2}')

    assert outbound
    assert all(isinstance(value, bytes) for value in outbound)
    assert all(native._validate_canonical_wire_bridge_envelope(value).ok for value in outbound)


def test_d542_protobuf_nack_without_error_uses_bridge_fallback_issue():
    nack_without_error = bytes.fromhex(
        "0a0273311210080110001a0473313a312001280138013200"
    )
    assert native._validate_canonical_wire_bridge_envelope(nack_without_error).ok

    graph = graphrefly.Graph("py-c1a-nack-fallback")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    source = graph.state(b'{"n":1}', name="edge-a")
    graphrefly.wire_edge_group(
        graph,
        bridge,
        name="group",
        outbound_edges={"edge-a": source},
    )
    issues: list[graphrefly.WireBridgeIssue] = []

    with bridge.issues.subscribe(
        lambda msg: issues.append(msg.value)
        if isinstance(msg, graphrefly.DataMessage)
        else None
    ):
        source.set(b'{"n":2}')
        protobuf.inbound_bytes.set(nack_without_error)

    assert issues[-1] == graphrefly.WireBridgeIssue(
        code="bridge_error",
        message="remote nack",
    )
