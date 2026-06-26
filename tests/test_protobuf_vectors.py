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
    ("path", "message", "validator", "empty_value_ids"),
    [
        (
            FIXTURE_DIR / "wire_bridge_envelope.v1.jsonl",
            "WireBridgeEnvelope",
            native._validate_canonical_wire_bridge_envelope,
            {"positive.data.empty_value"},
        ),
        (
            FIXTURE_DIR / "wire_edge_frame.v1.jsonl",
            "WireEdgeFrame",
            native._validate_canonical_wire_edge_frame,
            {"positive.wire_edge.data_empty_value"},
        ),
    ],
)
def test_d497_canonical_protobuf_vectors(
    path: Path,
    message: str,
    validator: Callable[[bytes], object],
    empty_value_ids: set[str],
) -> None:
    records = _load_vectors(path, message)
    assert any(record["canonical"] for record in records)
    assert any(not record["canonical"] for record in records)

    for record in records:
        result = validator(bytes.fromhex(record["hex"]))
        if record["canonical"]:
            assert result.ok is True
            assert result.category is None
            assert result.message is None
            if record["id"] in empty_value_ids:
                assert record["description"]
        else:
            assert result.ok is False
            assert result.category == record["errorCategory"]
            assert result.message


def test_d523_public_wire_bridge_protobuf_facade_reports_bytes_status_and_issues():
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

    def record_status(msg: graphrefly.Message[object]) -> None:
        if isinstance(msg, graphrefly.DataMessage):
            statuses.append(msg.value)

    def record_issue(msg: graphrefly.Message[object]) -> None:
        if isinstance(msg, graphrefly.DataMessage):
            issues.append(msg.value)

    with (
        protobuf.status.subscribe(record_status),
        protobuf.issues.subscribe(record_issue),
    ):
        protobuf.inbound_bytes.set(bytes.fromhex(str(positive["hex"])))
        protobuf.inbound_bytes.set(bytes.fromhex(str(negative["hex"])))

    assert graphrefly.WireBridgeProtobufStatus("inbound", "valid") in statuses
    assert statuses[-1] == graphrefly.WireBridgeProtobufStatus("inbound", "invalid")
    assert issues[-1].category == "unknown_field"
    assert "WireBridgeEnvelope" in issues[-1].message
