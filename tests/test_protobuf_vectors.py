import json
from collections.abc import Callable
from pathlib import Path

import pytest

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
