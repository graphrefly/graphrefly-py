from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

import pytest

import graphrefly
import graphrefly._native as native
from graphrefly import DataMessage, Graph, GraphReflyRuntimeError, Message

WireEdgeKind = Literal["dirty", "data"]


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _key(field_no: int, wire_type: int) -> bytes:
    return _varint((field_no << 3) | wire_type)


def _varint_field(field_no: int, value: int) -> bytes:
    return _key(field_no, 0) + _varint(value)


def _bytes_field(field_no: int, value: bytes) -> bytes:
    return _key(field_no, 2) + _varint(len(value)) + value


def _string_field(field_no: int, value: str) -> bytes:
    return _bytes_field(field_no, value.encode())


def _message_field(field_no: int, value: bytes) -> bytes:
    return _bytes_field(field_no, value)


def _metadata(seq: int, session_id: str) -> bytes:
    return b"".join(
        [
            _varint_field(1, seq),
            _varint_field(2, 0),
            _string_field(3, f"{session_id}:{seq}"),
            _varint_field(4, 1),
            _varint_field(5, 1),
        ]
    )


def _wire_edge_frame(
    kind: WireEdgeKind,
    edge_id: str,
    cause_id: str,
    value: bytes | None = None,
) -> bytes:
    encoded = [
        _varint_field(1, 1 if kind == "dirty" else 2),
        _string_field(2, edge_id),
        _string_field(3, cause_id),
    ]
    if value is not None:
        encoded.append(_bytes_field(4, value))
    return b"".join(encoded)


def _wire_edge_envelope(
    seq: int,
    kind: WireEdgeKind,
    edge_id: str,
    cause_id: str,
    value: bytes | None = None,
    *,
    session_id: str = "s1",
    validate: bool = True,
) -> bytes:
    frame = _wire_edge_frame(kind, edge_id, cause_id, value)
    data_payload = _message_field(2, frame)
    envelope = b"".join(
        [
            _string_field(1, session_id),
            _message_field(2, _metadata(seq, session_id)),
            _message_field(4, data_payload),
        ]
    )
    if validate:
        result = native._validate_canonical_wire_bridge_envelope(envelope)
        assert result.ok, result.message
    return envelope


def _close_envelope(seq: int, *, session_id: str = "s1") -> bytes:
    envelope = b"".join(
        [
            _string_field(1, session_id),
            _message_field(2, _metadata(seq, session_id)),
            _message_field(9, b""),
        ]
    )
    result = native._validate_canonical_wire_bridge_envelope(envelope)
    assert result.ok, result.message
    return envelope


def _record_data(target: list[object]):
    def record(message: Message[object]) -> None:
        if isinstance(message, DataMessage):
            target.append(message.value)

    return record


def _send_frames(
    protobuf: graphrefly.WireBridgeProtobuf,
    frames: list[tuple[WireEdgeKind, str, str, bytes | None]],
    *,
    start_seq: int = 1,
) -> None:
    for offset, (kind, edge_id, cause_id, value) in enumerate(frames):
        protobuf.inbound_bytes.set(
            _wire_edge_envelope(start_seq + offset, kind, edge_id, cause_id, value)
        )


def _complete_two_edge_cause(
    protobuf: graphrefly.WireBridgeProtobuf,
    cause_id: str,
    *,
    start_seq: int = 1,
    a_value: bytes = b"a",
    b_value: bytes = b"b",
) -> None:
    _send_frames(
        protobuf,
        [
            ("dirty", "a", cause_id, None),
            ("dirty", "b", cause_id, None),
            ("data", "a", cause_id, a_value),
            ("data", "b", cause_id, b_value),
        ],
        start_seq=start_seq,
    )


def _group_graph(
    name: str,
) -> tuple[Graph, graphrefly.WireBridgeProtobuf, graphrefly.WireEdgeGroup]:
    graph = Graph(name)
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    group = graphrefly.wire_edge_group(graph, bridge, inbound_edges=["a", "b"], name="group")
    return graph, protobuf, group


def test_public_inbound_group_gates_until_all_expected_dirty_and_data() -> None:
    _graph, protobuf, group = _group_graph("py-c1b-inbound-gate")
    a_values: list[object] = []
    b_values: list[object] = []
    statuses: list[object] = []
    issues: list[object] = []

    with (
        group.inbound_edges["a"].subscribe(_record_data(a_values)),
        group.inbound_edges["b"].subscribe(_record_data(b_values)),
        group.status.subscribe(_record_data(statuses)),
        group.issues.subscribe(_record_data(issues)),
    ):
        protobuf.inbound_bytes.set(_wire_edge_envelope(1, "dirty", "a", "c1"))
        protobuf.inbound_bytes.set(_wire_edge_envelope(2, "data", "a", "c1", b"A1"))
        assert a_values == []
        assert b_values == []

        protobuf.inbound_bytes.set(_wire_edge_envelope(3, "dirty", "b", "c1"))
        assert a_values == []
        assert b_values == []

        protobuf.inbound_bytes.set(_wire_edge_envelope(4, "data", "b", "c1", b"B1"))

    assert a_values == [b"A1"]
    assert b_values == [b"B1"]
    assert issues == []
    assert isinstance(statuses[-1], graphrefly.WireEdgeGroupStatus)
    assert statuses[-1].state == "released"
    assert statuses[-1].released == 2
    assert statuses[-1].active_cause_id is None
    assert statuses[-1].dirty == 0
    assert statuses[-1].data == 0


def test_public_outbound_group_emits_dirty_and_data_wire_edge_frames_for_one_cause() -> None:
    graph = Graph("py-c1b-outbound")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    edge_a = graph.state(b"A0", name="edge/a")
    edge_b = graph.state(b"B0", name="edge/b")
    graphrefly.wire_edge_group(
        graph,
        bridge,
        outbound_edges={"a": edge_a, "b": edge_b},
        name="group",
    )
    outbound: list[object] = []

    with protobuf.outbound_bytes.subscribe(_record_data(outbound)):
        outbound.clear()
        graph.batch(lambda: (edge_a.set(b"A1"), edge_b.set(b"B1")))

    assert len(outbound) == 4
    assert all(isinstance(value, bytes) for value in outbound)
    assert all(native._validate_canonical_wire_bridge_envelope(value).ok for value in outbound)
    dirty_count = sum(
        1
        for value in outbound
        if b"\x08\x01\x12\x01a" in value or b"\x08\x01\x12\x01b" in value
    )
    data_count = sum(
        1
        for value in outbound
        if b"\x08\x02\x12\x01a" in value or b"\x08\x02\x12\x01b" in value
    )
    assert dirty_count == 2
    assert data_count == 2


def test_public_outbound_group_d561_initial_bootstrap_current_replay_and_same_bytes() -> None:
    graph = Graph("py-d561-outbound-fresh-source")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    edge_a = graph.state(b"A0", name="edge/a")
    edge_b = graph.state(b"B0", name="edge/b")
    outbound: list[object] = []

    with protobuf.outbound_bytes.subscribe(_record_data(outbound)):
        graphrefly.wire_edge_group(
            graph,
            bridge,
            outbound_edges={"a": edge_a, "b": edge_b},
            name="group",
        )
        assert len(outbound) == 4
        assert all(isinstance(value, bytes) for value in outbound)
        assert sum(b"group:cause:1" in value for value in outbound if isinstance(value, bytes)) == 4
        outbound.clear()

        late_current: list[object] = []
        with protobuf.outbound_bytes.subscribe(_record_data(late_current)):
            assert len(late_current) == 1

        assert (
            outbound == []
        ), "D561: late-subscriber/current replay must not admit a new outbound cause"

        graph.batch(lambda: (edge_a.set(b"A0"), edge_b.set(b"B0")))

    assert len(outbound) == 4
    assert all(isinstance(value, bytes) for value in outbound)
    assert sum(b"group:cause:2" in value for value in outbound if isinstance(value, bytes)) == 4
    assert any(b"\x08\x02\x12\x01a" in value and b"A0" in value for value in outbound)
    assert any(b"\x08\x02\x12\x01b" in value and b"B0" in value for value in outbound)


@pytest.mark.parametrize(
    ("name", "frames", "expected_code"),
    [
        ("unknown-edge", [("dirty", "z", "c1", None)], "unknown_edge"),
        (
            "duplicate-dirty",
            [("dirty", "a", "c1", None), ("dirty", "a", "c1", None)],
            "duplicate_dirty",
        ),
        (
            "duplicate-data",
            [
                ("dirty", "a", "c1", None),
                ("dirty", "b", "c1", None),
                ("data", "a", "c1", b"one"),
                ("data", "a", "c1", b"two"),
            ],
            "duplicate_data",
        ),
        ("data-before-dirty", [("data", "a", "c1", b"one")], "data_before_dirty"),
        (
            "competing-cause",
            [("dirty", "a", "c1", None), ("dirty", "b", "c2", None)],
            "competing_cause",
        ),
    ],
)
def test_public_inbound_fail_closed_cases_are_issues_not_protocol_terminals(
    name: str,
    frames: list[tuple[WireEdgeKind, str, str, bytes | None]],
    expected_code: str,
) -> None:
    _graph, protobuf, group = _group_graph(f"py-c1b-{name}")
    a_values: list[object] = []
    issues: list[object] = []
    statuses: list[object] = []

    with (
        group.inbound_edges["a"].subscribe(_record_data(a_values)),
        group.issues.subscribe(_record_data(issues)),
        group.status.subscribe(_record_data(statuses)),
    ):
        _send_frames(protobuf, frames)

    assert a_values == []
    assert any(
        isinstance(issue, graphrefly.WireEdgeGroupIssue) and issue.code == expected_code
        for issue in issues
    )
    assert isinstance(statuses[-1], graphrefly.WireEdgeGroupStatus)
    assert statuses[-1].state == "issues"
    assert group.issues.status != "errored"
    assert group.status.status != "errored"
    assert group.inbound_edges["a"].status != "errored"
    assert group.inbound_edges["a"].status != "complete"


def test_malformed_and_incomplete_cause_are_issues_not_protocol_terminals() -> None:
    _graph, protobuf, group = _group_graph("py-c1b-malformed-incomplete")
    issues: list[object] = []
    statuses: list[object] = []

    with (
        group.issues.subscribe(_record_data(issues)),
        group.status.subscribe(_record_data(statuses)),
    ):
        protobuf.inbound_bytes.set(
            _wire_edge_envelope(
                1,
                "data",
                "a",
                "c1",
                None,
                validate=False,
            )
        )
        protobuf.inbound_bytes.set(_wire_edge_envelope(2, "dirty", "a", "c2"))
        protobuf.inbound_bytes.set(_close_envelope(3))

    codes = [issue.code for issue in issues if isinstance(issue, graphrefly.WireEdgeGroupIssue)]
    assert "malformed_frame" in codes
    assert "incomplete_cause" in codes
    assert statuses[-1].state == "issues"
    assert group.issues.status != "errored"
    assert group.status.status != "errored"
    assert group.inbound_edges["a"].status != "errored"
    assert group.inbound_edges["a"].status != "complete"


def test_recent_replay_after_release_is_issue_not_second_release() -> None:
    _graph, protobuf, group = _group_graph("py-c1b-recent-replay")
    a_values: list[object] = []
    issues: list[object] = []
    statuses: list[object] = []

    with (
        group.inbound_edges["a"].subscribe(_record_data(a_values)),
        group.issues.subscribe(_record_data(issues)),
        group.status.subscribe(_record_data(statuses)),
    ):
        _complete_two_edge_cause(protobuf, "c1", a_value=b"A1", b_value=b"B1")
        _complete_two_edge_cause(protobuf, "c1", start_seq=5, a_value=b"A2", b_value=b"B2")

    assert a_values == [b"A1"]
    codes = [issue.code for issue in issues if isinstance(issue, graphrefly.WireEdgeGroupIssue)]
    assert any(code in {"duplicate_dirty", "duplicate_data"} for code in codes)
    assert statuses[-1].state == "issues"
    assert statuses[-1].released == 2


def test_failed_cause_replay_is_issue_not_resurrection() -> None:
    _graph, protobuf, group = _group_graph("py-c1b-failed-replay")
    a_values: list[object] = []
    b_values: list[object] = []
    issues: list[object] = []
    statuses: list[object] = []

    with (
        group.inbound_edges["a"].subscribe(_record_data(a_values)),
        group.inbound_edges["b"].subscribe(_record_data(b_values)),
        group.issues.subscribe(_record_data(issues)),
        group.status.subscribe(_record_data(statuses)),
    ):
        _send_frames(
            protobuf,
            [
                ("dirty", "a", "c1", None),
                ("dirty", "b", "c1", None),
                ("data", "a", "c1", b"A1"),
                ("data", "a", "c1", b"A2"),
                ("data", "b", "c1", b"B1"),
                ("dirty", "a", "c1", None),
                ("dirty", "b", "c1", None),
                ("data", "a", "c1", b"A3"),
                ("data", "b", "c1", b"B2"),
            ],
        )

    assert a_values == []
    assert b_values == []
    codes = [issue.code for issue in issues if isinstance(issue, graphrefly.WireEdgeGroupIssue)]
    assert "duplicate_data" in codes
    assert "incomplete_cause" in codes
    assert statuses[-1].state == "issues"
    assert statuses[-1].active_cause_id is None
    assert statuses[-1].dirty == 0
    assert statuses[-1].data == 0
    assert group.issues.status != "errored"
    assert group.status.status != "errored"


def test_replay_tombstone_is_bounded_recent_memory_without_public_limit() -> None:
    graph = Graph("py-c1b-bounded-replay")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    group = graphrefly.wire_edge_group(graph, bridge, inbound_edges=["a"], name="group")
    a_values: list[object] = []
    issues: list[object] = []
    seq = 1

    def release(cause_id: str, value: bytes) -> None:
        nonlocal seq
        protobuf.inbound_bytes.set(_wire_edge_envelope(seq, "dirty", "a", cause_id))
        seq += 1
        protobuf.inbound_bytes.set(_wire_edge_envelope(seq, "data", "a", cause_id, value))
        seq += 1

    with (
        group.inbound_edges["a"].subscribe(_record_data(a_values)),
        group.issues.subscribe(_record_data(issues)),
    ):
        release("c1", b"first")
        protobuf.inbound_bytes.set(_wire_edge_envelope(seq, "dirty", "a", "c1"))
        seq += 1
        assert any(
            isinstance(issue, graphrefly.WireEdgeGroupIssue)
            and issue.cause_id == "c1"
            and issue.code == "duplicate_dirty"
            for issue in issues
        )

        for index in range(2, 1100):
            release(f"c{index}", bytes([index % 256]))
        released_before_old_replay = len(a_values)
        release("c1", b"after-many")

    assert len(a_values) == released_before_old_replay + 1
    assert a_values[-1] == b"after-many"


def test_subscription_order_variants_do_not_change_release_or_issue_visibility() -> None:
    _graph, protobuf, group = _group_graph("py-c1b-subscription-order")
    status_first: list[object] = []
    issues_first: list[object] = []
    a_late: list[object] = []

    with (
        group.status.subscribe(_record_data(status_first)),
        group.issues.subscribe(_record_data(issues_first)),
    ):
        _complete_two_edge_cause(protobuf, "c1", a_value=b"A1", b_value=b"B1")
        with group.inbound_edges["a"].subscribe(_record_data(a_late)):
            pass

    assert a_late == [b"A1"]
    assert issues_first == []
    assert status_first[-1].state == "released"

    graph2, protobuf2, group2 = _group_graph("py-c1b-subscription-order-late-status")
    a_first: list[object] = []
    status_late: list[object] = []
    issues_late: list[object] = []
    with group2.inbound_edges["a"].subscribe(_record_data(a_first)):
        _complete_two_edge_cause(protobuf2, "c1", a_value=b"A1", b_value=b"B1")
        with (
            group2.status.subscribe(_record_data(status_late)),
            group2.issues.subscribe(_record_data(issues_late)),
        ):
            pass

    assert graph2.describe()["name"] == "py-c1b-subscription-order-late-status"
    assert a_first == [b"A1"]
    assert status_late[-1].state == "released"
    assert issues_late == []

    graph3, protobuf3, group3 = _group_graph("py-c1b-subscription-order-zero-public")
    _complete_two_edge_cause(protobuf3, "c1", a_value=b"A1", b_value=b"B1")
    a_zero_public: list[object] = []
    status_zero_public: list[object] = []
    issues_zero_public: list[object] = []
    with (
        group3.inbound_edges["a"].subscribe(_record_data(a_zero_public)),
        group3.status.subscribe(_record_data(status_zero_public)),
        group3.issues.subscribe(_record_data(issues_zero_public)),
    ):
        pass

    assert graph3.describe()["name"] == "py-c1b-subscription-order-zero-public"
    assert a_zero_public == [b"A1"]
    assert status_zero_public[-1].state == "released"
    assert issues_zero_public == []


def test_release_is_idempotent_child_only_cascades_from_bridge_and_emits_no_terminal() -> None:
    graph = Graph("py-c1b-release")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    group = graphrefly.wire_edge_group(graph, bridge, inbound_edges=["a"], name="group")
    outbound_seen: list[Message[bytes]] = []

    with protobuf.outbound_bytes.subscribe(outbound_seen.append):
        group.release()
        group.release()

    terminal_kinds = {"COMPLETE", "ERROR", "TEARDOWN"}
    assert not any(message.kind in terminal_kinds for message in outbound_seen)
    assert not _describe_has_prefix(graph.describe(), "group/")
    assert not _describe_has_prefix(graph.describe(), "group.")

    replacement = graphrefly.wire_edge_group(graph, bridge, inbound_edges=["b"], name="group2")
    replacement.release()
    assert not _describe_has_prefix(graph.describe(), "group2/")
    assert not _describe_has_prefix(graph.describe(), "group2.")
    assert protobuf.inbound_bytes.has_value is False

    live_child = graphrefly.wire_edge_group(graph, bridge, inbound_edges=["c"], name="group3")
    bridge.release()
    assert not _describe_has_prefix(graph.describe(), "group3/")
    assert not _describe_has_prefix(graph.describe(), "group3.")
    live_child.release()
    protobuf.release()
    bridge.release()

    with pytest.raises(GraphReflyRuntimeError, match="released"):
        graphrefly.wire_edge_group(graph, bridge, inbound_edges=["late"])


def test_describe_exposes_wire_edge_group_adapter_lanes() -> None:
    graph = Graph("py-c1b-describe")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    edge_a = graph.state(b"A0", name="edge/a")
    edge_b = graph.state(b"B0", name="edge/b")
    graphrefly.wire_edge_group(graph, bridge, outbound_edges={"a": edge_a, "b": edge_b}, name="out")
    graphrefly.wire_edge_group(graph, bridge, inbound_edges=["a", "b"], name="in")
    protobuf.outbound_bytes.subscribe(lambda _msg: None).unsubscribe()
    protobuf.status.subscribe(lambda _msg: None).unsubscribe()
    snapshot = graph.describe()

    names = {node["name"] for node in _walk_nodes(snapshot)}
    edges = {(edge["from"], edge["to"]) for edge in _walk_edges(snapshot)}

    assert {"out/events", "out/gate", "out/commands", "out/status", "out/issues"} <= names
    assert {"in/events", "in/gate", "in/status", "in/issues", "in/inbound/a"} <= names
    assert ("bridge/inbound", "in/events") in edges
    assert ("in/events", "in/gate") in edges
    assert ("in/events", "in/issues") in edges
    assert ("in/gate", "in/issues") in edges
    assert ("in/events", "in/status") in edges
    assert ("in/gate", "in/status") in edges
    assert ("in/gate", "in/inbound/a") in edges
    assert ("in/inbound/a", "in/py/inbound/a") in edges
    assert ("edge/a", "out/py/outbound/a") in edges
    assert ("out/py/outbound/a", "out/events") in edges
    assert ("out/events", "out/gate") in edges
    assert ("out/events", "out/commands") in edges
    assert ("out/events", "out/issues") in edges
    assert ("out/gate", "out/issues") in edges
    assert ("out/events", "out/status") in edges
    assert ("out/gate", "out/status") in edges
    assert ("out/commands", "bridge/command") in edges


def _walk_nodes(snapshot: dict[str, object]) -> Iterator[dict[str, object]]:
    for node in snapshot.get("nodes", []):
        if isinstance(node, dict):
            yield node
    subgraphs = snapshot.get("subgraphs")
    if not isinstance(subgraphs, list):
        return
    for subgraph in subgraphs:
        if isinstance(subgraph, dict):
            yield from _walk_nodes(subgraph)


def _walk_edges(snapshot: dict[str, object]) -> Iterator[dict[str, str]]:
    for edge in snapshot.get("edges", []):
        if isinstance(edge, dict):
            yield edge
    subgraphs = snapshot.get("subgraphs")
    if not isinstance(subgraphs, list):
        return
    for subgraph in subgraphs:
        if isinstance(subgraph, dict):
            yield from _walk_edges(subgraph)


def _describe_has_prefix(snapshot: dict[str, object], prefix: str) -> bool:
    return any(str(node.get("name", "")).startswith(prefix) for node in _walk_nodes(snapshot))
