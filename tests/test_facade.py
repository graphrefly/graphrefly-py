import gc
from dataclasses import FrozenInstanceError, is_dataclass
from importlib import import_module
from inspect import signature
from typing import Any

import pytest

import graphrefly
from graphrefly import (
    CallbackError,
    ControlMessage,
    Ctx,
    DataMessage,
    ErrorMessage,
    Graph,
    GraphEvent,
    GraphReflyNoDataError,
    GraphReflyRestoreError,
    GraphReflyRuntimeError,
    GraphReflyValueError,
    Message,
    RestoreContext,
    RestoreDescriptor,
    RestoreRef,
    Retain,
    RewireNext,
    SubscriberCallbackError,
    Subscription,
    restore_graph,
    restore_ref,
    restore_registry,
)


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


def _metadata(seq: int, session_id: str, *, ack_for_seq: int | None = None) -> bytes:
    encoded = [
        _varint_field(1, seq),
        _varint_field(2, 0),
        _string_field(3, f"{session_id}:{seq}"),
        _varint_field(4, 1),
        _varint_field(5, 2),
    ]
    if ack_for_seq is not None:
        encoded.append(_varint_field(7, ack_for_seq))
    return b"".join(encoded)


def _ack_envelope(seq: int, ack_for_seq: int, *, session_id: str = "s1") -> bytes:
    return b"".join(
        [
            _string_field(1, session_id),
            _message_field(2, _metadata(seq, session_id, ack_for_seq=ack_for_seq)),
            _message_field(5, b""),
        ]
    )


def _record_data(target: list[Any]):
    def record(message: Message[object]) -> None:
        if isinstance(message, DataMessage):
            target.append(message.value)

    return record


def _describe_has_prefix(snapshot: dict[str, object], *prefixes: str) -> bool:
    nodes = snapshot.get("nodes", [])
    if not isinstance(nodes, list):
        return False
    for node in nodes:
        if isinstance(node, dict):
            name = node.get("name")
            if isinstance(name, str) and name.startswith(prefixes):
                return True
    return False


def test_import_package_surface():
    assert graphrefly.__version__ == "0.21.0a0"
    assert graphrefly.version() == "0.21.0a0"
    assert Graph("smoke").describe()["name"] == "smoke"
    assert graphrefly.DataIssue("missing", "reserved").code == "missing"
    assert graphrefly.Ctx is Ctx
    assert graphrefly.RewireNext is RewireNext
    assert graphrefly.Retain is Retain
    assert graphrefly.RestoreContext is RestoreContext
    assert graphrefly.RestoreDescriptor is RestoreDescriptor
    assert graphrefly.RestoreRef is RestoreRef
    assert graphrefly.restore_graph is restore_graph
    assert graphrefly.restore_ref is restore_ref
    assert graphrefly.restore_registry is restore_registry
    assert graphrefly.wire_bridge
    assert graphrefly.wire_bridge_protobuf
    assert graphrefly.wire_edge_group
    assert graphrefly.wire_bridge_ack_driver
    assert issubclass(graphrefly.GraphReflyCheckpointError, GraphReflyValueError)
    assert issubclass(graphrefly.GraphReflyRestoreError, GraphReflyRuntimeError)
    assert issubclass(graphrefly.GraphReflyNoDataError, LookupError)
    assert hasattr(import_module("graphrefly._native"), "Graph")
    assert "_conformance" not in graphrefly.__all__


def test_public_facade_has_no_equals_substitution_surface():
    graph = Graph("py-public-no-equals")
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    assert "equals" not in signature(Graph.node).parameters
    assert "equals" not in signature(Graph.derived).parameters
    assert "distinctUntilChanged" not in graphrefly.__all__
    assert not hasattr(graphrefly, "distinctUntilChanged")

    with source.subscribe(seen.append):
        seen.clear()
        source.set(1)
        source.set(1)

    assert [message.kind for message in seen] == ["DIRTY", "DATA", "DIRTY", "DATA"]
    assert [message.value for message in seen if isinstance(message, DataMessage)] == [1, 1]


def test_public_facade_has_no_raw_wire_bridge_or_wire_edge_group_surface():
    native = import_module("graphrefly._native")
    forbidden = {
        "WireEdgeGroupBundle",
        "WireEdgeGroupEdge",
        "WireEdgeGroupOptions",
        "WireEdgeGroupIssueCode",
        "WireEdgeGroupStatusState",
        "WireBridgeBundle",
        "WireBridgeAck",
        "WireBridgeAttempt",
        "WireBridgeInbound",
        "WireBridgeIngress",
        "WireBridgeEnvelope",
        "WireBridgeEnvelopeInput",
        "WireBridgeEnvelopeType",
        "WireBridgeMetadata",
        "WireBridgeNack",
        "WireBridgeOptions",
        "WireBridgePayload",
        "WireBridgeReceipt",
        "WireBridgeStatusState",
        "WireEdgeFrame",
        "WireBridgeCommand",
        "WireBridgeEvent",
        "CanonicalWireBridgeEnvelope",
        "CanonicalWireEdgeFrame",
        "CanonicalProtobufRoundtrip",
        "CanonicalProtobufValidation",
        "PyO3Handle",
        "_roundtrip_canonical_wire_bridge_envelope",
        "_roundtrip_canonical_wire_edge_frame",
        "_validate_canonical_wire_bridge_envelope",
        "_validate_canonical_wire_edge_frame",
    }

    for name in forbidden:
        assert name not in graphrefly.__all__
        assert not hasattr(graphrefly, name)
        if not name.startswith("_") and not name.startswith("CanonicalProtobuf"):
            assert not hasattr(native, name)

    assert hasattr(native, "_roundtrip_canonical_wire_bridge_envelope")
    assert hasattr(native, "_roundtrip_canonical_wire_edge_frame")
    assert hasattr(native, "_validate_canonical_wire_bridge_envelope")
    assert hasattr(native, "_validate_canonical_wire_edge_frame")
    assert not hasattr(graphrefly.Node, "up")
    assert not hasattr(graphrefly.Node, "down")
    assert not hasattr(graphrefly.Ctx, "up")
    assert not hasattr(graphrefly.Ctx, "down")


def test_wire_bridge_public_facade_shape_and_dataclasses():
    for cls in [
        graphrefly.WireBridgeStatus,
        graphrefly.WireBridgeIssue,
        graphrefly.WireEdgeGroupStatus,
        graphrefly.WireEdgeGroupIssue,
        graphrefly.WireBridgeProtobufStatus,
        graphrefly.WireBridgeProtobufIssue,
        graphrefly.WireBridgeAckTimeout,
        graphrefly.WireBridgeAckDriverStatus,
        graphrefly.WireBridgeAckDriverIssue,
    ]:
        assert is_dataclass(cls)
        if cls is graphrefly.WireBridgeStatus:
            assert "__dict__" not in cls(state="idle", session_id="s").__class__.__slots__

    status = graphrefly.WireBridgeStatus(state="idle", session_id="s")
    with pytest.raises(FrozenInstanceError):
        status.state = "open"  # type: ignore[misc]

    graph = Graph("py-c1-shape")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    group = graphrefly.wire_edge_group(graph, bridge, name="group", inbound_edges=["a"])
    clock = graph.state(0, name="clock")
    ack = graphrefly.wire_bridge_ack_driver(
        graph,
        bridge,
        clock=clock,
        timeout_ms=5,
        name="ack",
    )

    assert not hasattr(bridge, "close")
    assert set(group.inbound_edges) == {"a"}
    assert protobuf.inbound_bytes.has_value is False
    with ack.status.subscribe(lambda _msg: None):
        clock.set(1)
        assert ack.status.cache().timeout_ms == 5
    ack.release()
    group.release()
    protobuf.release()
    bridge.release()
    bridge.release()


def test_wire_bridge_public_constructor_guards():
    graph = Graph("py-c1-guards")
    bridge = graphrefly.wire_bridge(graph, session_id="s1")

    with pytest.raises(GraphReflyValueError, match="exactly one"):
        graphrefly.wire_edge_group(graph, bridge)
    with pytest.raises(GraphReflyValueError, match="exactly one"):
        graphrefly.wire_edge_group(
            graph,
            bridge,
            inbound_edges=["a"],
            outbound_edges={"b": graph.state(b"b")},
        )
    outbound = graphrefly.wire_edge_group(
        graph,
        bridge,
        outbound_edges={"a": graph.state(b"a")},
    )
    assert outbound.inbound_edges == {}
    outbound.release()

    bridge.release()
    with pytest.raises(GraphReflyRuntimeError, match="released"):
        graphrefly.wire_bridge_protobuf(graph, bridge)
    with pytest.raises(GraphReflyRuntimeError, match="released"):
        graphrefly.wire_edge_group(graph, bridge, inbound_edges=["a"])
    with pytest.raises(GraphReflyRuntimeError, match="released"):
        graphrefly.wire_bridge_ack_driver(
            graph,
            bridge,
            clock=graph.state(0),
            timeout_ms=5,
        )


def test_wire_bridge_ack_driver_invalid_clock_is_issue_not_timeout():
    graph = Graph("py-c1-ack-invalid-clock")
    bridge = graphrefly.wire_bridge(graph, session_id="s1")
    clock = graph.state(0, name="clock")
    ack = graphrefly.wire_bridge_ack_driver(graph, bridge, clock=clock, timeout_ms=5)
    timeouts: list[graphrefly.WireBridgeAckTimeout] = []
    issues: list[graphrefly.WireBridgeAckDriverIssue] = []

    def record_timeout(msg: Message[object]) -> None:
        if isinstance(msg, DataMessage):
            timeouts.append(msg.value)

    def record_issue(msg: Message[object]) -> None:
        if isinstance(msg, DataMessage):
            issues.append(msg.value)

    with ack.timeouts.subscribe(record_timeout), ack.issues.subscribe(record_issue):
        timeouts.clear()
        issues.clear()
        clock.set("bad")  # type: ignore[arg-type]

    assert timeouts == []
    assert issues == [
        graphrefly.WireBridgeAckDriverIssue(
            code="invalid_clock",
            message="wire_bridge_ack_driver clock facts must be non-negative integers",
        )
    ]

    with ack.issues.subscribe(record_issue):
        issues.clear()
        clock.set(10)
        clock.set(9)

    assert issues == [
        graphrefly.WireBridgeAckDriverIssue(
            code="invalid_clock",
            message="wire_bridge_ack_driver clock facts must be monotonic non-decreasing",
        )
    ]


def test_c1c_ack_driver_clock_driven_timeout_retries_and_exhausts():
    graph = Graph("py-c1c-ack-driver-retry-exhaust")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    clock = graph.state(1000, name="clock")
    ack = graphrefly.wire_bridge_ack_driver(graph, bridge, clock=clock, timeout_ms=5, name="ack")
    source = graph.state(b"first", name="edge-source")
    graphrefly.wire_edge_group(graph, bridge, outbound_edges={"edge-a": source}, name="group")
    timeouts: list[graphrefly.WireBridgeAckTimeout] = []
    ack_statuses: list[graphrefly.WireBridgeAckDriverStatus] = []
    bridge_statuses: list[graphrefly.WireBridgeStatus] = []
    bridge_issues: list[graphrefly.WireBridgeIssue] = []
    outbound: list[bytes] = []

    with (
        ack.timeouts.subscribe(_record_data(timeouts)),
        ack.status.subscribe(_record_data(ack_statuses)),
        bridge.status.subscribe(_record_data(bridge_statuses)),
        bridge.issues.subscribe(_record_data(bridge_issues)),
        protobuf.outbound_bytes.subscribe(_record_data(outbound)),
    ):
        assert len(outbound) == 1
        clock.set(1004)
        assert timeouts == []
        clock.set(1005)
        assert any(timeout.attempt == 1 and timeout.observed_at_ms == 1005 for timeout in timeouts)
        assert len(outbound) > 1
        assert any(status.state == "waiting" for status in bridge_statuses)
        clock.set(1010)

    assert any(timeout.attempt == 2 and timeout.observed_at_ms == 1010 for timeout in timeouts)
    assert any(status.state == "exhausted" for status in bridge_statuses)
    assert bridge_issues[-1].code == "bridge_error"
    assert "ack timeout for seq" in bridge_issues[-1].message
    assert ack_statuses[-1].last_timeout == timeouts[-1]


def test_c1c_stale_mismatched_and_malformed_timeout_ingress_fail_closed():
    graph = Graph("py-c1c-ack-driver-private-ingress")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    clock = graph.state(1000, name="clock")
    ack = graphrefly.wire_bridge_ack_driver(graph, bridge, clock=clock, timeout_ms=5, name="ack")
    source = graph.state(b"first", name="edge-source")
    graphrefly.wire_edge_group(graph, bridge, outbound_edges={"edge-a": source}, name="group")
    bridge_statuses: list[graphrefly.WireBridgeStatus] = []
    bridge_issues: list[graphrefly.WireBridgeIssue] = []

    with (
        bridge.status.subscribe(_record_data(bridge_statuses)),
        bridge.issues.subscribe(_record_data(bridge_issues)),
        protobuf.outbound_bytes.subscribe(lambda _msg: None),
    ):
        ack._native._conformance_ack_timeout(1, 2, 1005)  # noqa: SLF001
        assert bridge_issues == []
        assert bridge_statuses[-1].pending >= 1

        ack._native._conformance_ack_timeout(1, 0, 1005)  # noqa: SLF001
        ack._native._conformance_ack_timeout(0, 1, 1005)  # noqa: SLF001

        protobuf.inbound_bytes.set(_ack_envelope(1, 1))
        ack._native._conformance_ack_timeout(1, 1, 1005)  # noqa: SLF001

    assert [issue.message for issue in bridge_issues] == [
        "wireBridge: ack-timeout command attempt must be positive",
        "wireBridge: ack-timeout command seq must be positive",
    ]
    assert not any(status.state == "exhausted" for status in bridge_statuses)


def test_c1c_ack_driver_release_detaches_private_command_source():
    graph = Graph("py-c1c-ack-driver-release")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    clock = graph.state(1000, name="clock")
    ack = graphrefly.wire_bridge_ack_driver(graph, bridge, clock=clock, timeout_ms=5, name="ack")
    source = graph.state(b"first", name="edge-source")
    graphrefly.wire_edge_group(graph, bridge, outbound_edges={"edge-a": source}, name="group")
    timeouts: list[graphrefly.WireBridgeAckTimeout] = []
    outbound: list[bytes] = []

    with (
        ack.timeouts.subscribe(_record_data(timeouts)),
        protobuf.outbound_bytes.subscribe(_record_data(outbound)),
    ):
        assert len(outbound) == 1

    ack.release()
    clock.set(1005)

    assert timeouts == []
    assert len(outbound) == 1
    assert not _describe_has_prefix(graph.describe(), "ack/", "ack.")


def test_c1c_ack_driver_release_reports_scope_error_after_native_cleanup():
    graph = Graph("py-c1c-ack-driver-release-scope-error")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    clock = graph.state(1000, name="clock")
    ack = graphrefly.wire_bridge_ack_driver(graph, bridge, clock=clock, timeout_ms=5, name="ack")

    class FailingResource:
        def _close_from_graph(self) -> None:
            raise RuntimeError("scope cleanup failed")

    ack._scope.register(FailingResource())  # noqa: SLF001

    with pytest.raises(RuntimeError, match="scope cleanup failed"):
        ack.release()

    assert ack._released is True  # noqa: SLF001
    assert ack not in bridge._children  # noqa: SLF001
    assert not _describe_has_prefix(graph.describe(), "ack/", "ack.")


def test_c1c_subscription_order_variants_keep_timeout_issue_and_status_visible():
    graph = Graph("py-c1c-ack-driver-subscription-order")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    clock = graph.state(1000, name="clock")
    ack = graphrefly.wire_bridge_ack_driver(graph, bridge, clock=clock, timeout_ms=5, name="ack")
    source = graph.state(b"first", name="edge-source")
    graphrefly.wire_edge_group(graph, bridge, outbound_edges={"edge-a": source}, name="group")
    statuses: list[graphrefly.WireBridgeAckDriverStatus] = []

    with ack.status.subscribe(_record_data(statuses)), protobuf.outbound_bytes.subscribe(
        lambda _msg: None
    ):
        clock.set(1005)

    timeout = ack.timeouts.cache()
    assert timeout.attempt == 1
    assert timeout.observed_at_ms == 1005
    assert statuses[-1].last_timeout == timeout

    issue_graph = Graph("py-c1c-ack-driver-issue-order")
    issue_bridge = graphrefly.wire_bridge(issue_graph, session_id="s1")
    issue_clock = issue_graph.state(0, name="clock")
    issue_ack = graphrefly.wire_bridge_ack_driver(
        issue_graph,
        issue_bridge,
        clock=issue_clock,
        timeout_ms=5,
    )
    issues: list[graphrefly.WireBridgeAckDriverIssue] = []

    with issue_ack.issues.subscribe(_record_data(issues)):
        issue_clock.set("bad")  # type: ignore[arg-type]
    assert issues[-1].code == "invalid_clock"


def test_d542_bridge_release_cascades_children_and_late_child_release_is_noop():
    graph = Graph("py-c1-release-cascade")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    group = graphrefly.wire_edge_group(graph, bridge, inbound_edges=["edge-a"], name="group")
    ack = graphrefly.wire_bridge_ack_driver(
        graph,
        bridge,
        clock=graph.state(0, name="clock"),
        timeout_ms=5,
        name="ack",
    )

    bridge.release()
    ack.release()
    group.release()
    protobuf.release()

    with pytest.raises(GraphReflyRuntimeError, match="released"):
        graphrefly.wire_bridge_protobuf(graph, bridge)


def test_b100_explicit_child_release_closes_live_public_child_observers():
    graph = Graph("py-b100-explicit-child-release-live-observers")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    group = graphrefly.wire_edge_group(graph, bridge, inbound_edges=["edge-a"], name="group")
    ack = graphrefly.wire_bridge_ack_driver(
        graph,
        bridge,
        clock=graph.state(0, name="clock"),
        timeout_ms=5,
        name="ack",
    )

    protobuf_sub = protobuf.outbound_bytes.subscribe(lambda _msg: None)
    group_status_sub = group.status.subscribe(lambda _msg: None)
    group_edge_sub = group.inbound_edges["edge-a"].subscribe(lambda _msg: None)
    ack_sub = ack.timeouts.subscribe(lambda _msg: None)

    group.release()
    ack.release()
    protobuf.release()

    assert group_status_sub.closed is True
    assert group_edge_sub.closed is True
    assert ack_sub.closed is True
    assert protobuf_sub.closed is True
    assert group._released is True  # noqa: SLF001
    assert ack._released is True  # noqa: SLF001
    assert protobuf._released is True  # noqa: SLF001
    assert not _describe_has_prefix(
        graph.describe(),
        "group/",
        "group.",
        "ack/",
        "ack.",
        "protobuf/",
    )


def test_b100_live_graph_observer_survives_child_release():
    graph = Graph("py-b100-live-graph-observer-child-release")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    group = graphrefly.wire_edge_group(graph, bridge, inbound_edges=["edge-a"], name="group")
    unrelated = graph.state(0, name="unrelated")
    graph_events: list[GraphEvent] = []

    graph_observer = graph.observe(graph_events.append)
    group_status_sub = group.status.subscribe(lambda _msg: None)

    group.release()
    unrelated.set(1)

    assert group_status_sub.closed is True
    assert graph_observer.closed is False
    graph_observer.unsubscribe()
    assert group._released is True  # noqa: SLF001
    assert not _describe_has_prefix(graph.describe(), "group/", "group.")
    assert any(
        event.path == "unrelated" and event.message == DataMessage(1)
        for event in graph_events
    )
    assert not any(
        event.path.startswith("group")
        and isinstance(event.message, (ControlMessage, ErrorMessage))
        and event.message.kind in {"COMPLETE", "ERROR", "TEARDOWN"}
        for event in graph_events
    )


def test_b100_bridge_cascade_release_closes_live_public_child_observers():
    graph = Graph("py-b100-bridge-cascade-live-child-observers")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    group = graphrefly.wire_edge_group(graph, bridge, inbound_edges=["edge-a"], name="group")
    ack = graphrefly.wire_bridge_ack_driver(
        graph,
        bridge,
        clock=graph.state(0, name="clock"),
        timeout_ms=5,
        name="ack",
    )

    protobuf_sub = protobuf.status.subscribe(lambda _msg: None)
    group_sub = group.issues.subscribe(lambda _msg: None)
    ack_sub = ack.status.subscribe(lambda _msg: None)

    bridge.release()

    assert protobuf_sub.closed is True
    assert group_sub.closed is True
    assert ack_sub.closed is True
    assert protobuf._released is True  # noqa: SLF001
    assert group._released is True  # noqa: SLF001
    assert ack._released is True  # noqa: SLF001
    assert bridge._released is True  # noqa: SLF001
    assert bridge._children == []  # noqa: SLF001


def test_d542_wire_edge_group_outbound_non_bytes_is_public_issue_status():
    graph = Graph("py-c1-outbound-non-bytes")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    source = graph.state(b"ok", name="edge-source")
    group = graphrefly.wire_edge_group(
        graph,
        bridge,
        outbound_edges={"edge-a": source},
        name="group",
    )
    issues: list[graphrefly.WireEdgeGroupIssue] = []
    statuses: list[graphrefly.WireEdgeGroupStatus] = []

    with (
        group.issues.subscribe(
            lambda msg: issues.append(msg.value)
            if isinstance(msg, graphrefly.DataMessage)
            else None
        ),
        group.status.subscribe(
            lambda msg: statuses.append(msg.value)
            if isinstance(msg, graphrefly.DataMessage)
            else None
        ),
    ):
        source.set("not bytes")

    assert issues[-1].code == "malformed_frame"
    assert issues[-1].edge_id == "edge-a"
    assert "must emit bytes" in issues[-1].message
    assert statuses[-1].state == "issues"
    assert statuses[-1].last_issue == issues[-1]


def test_d559_wire_edge_group_outbound_bytearray_is_copied_to_bytes():
    graph = Graph("py-d559-bytearray-copy")
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    source = graph.state(bytearray(b"ok"), name="edge-source")
    graphrefly.wire_edge_group(
        graph,
        bridge,
        outbound_edges={"edge-a": source},
        name="group",
    )
    outbound: list[bytes] = []
    mutable = bytearray(b"first")

    with protobuf.outbound_bytes.subscribe(_record_data(outbound)):
        outbound.clear()
        source.set(mutable)
        mutable[:] = b"later"

    assert outbound
    assert all(isinstance(value, bytes) for value in outbound)
    assert any(b"first" in value for value in outbound)
    assert not any(b"later" in value for value in outbound)


def test_python_callback_runs_through_rust_graph_and_subscription_observes_wave():
    seen: list[Message[object]] = []
    graph = Graph("py-smoke")
    source = graph.state(1, name="source")
    plus_one = graph.derived([source], lambda value: value + 1, name="plus_one")

    with plus_one.subscribe(seen.append):
        assert plus_one.cache() == 2
        source.set(4)
        assert plus_one.cache() == 5
        assert plus_one.status in {"settled", "resolved"}

    assert DataMessage(2) in seen
    assert DataMessage(5) in seen


def test_callback_exception_becomes_graph_error_observation():
    seen: list[Message[object]] = []
    graph = Graph("py-error-smoke")
    source = graph.state(1, name="source")

    def boom(_value: int) -> int:
        raise ValueError("boom")

    bad = graph.derived([source], boom, name="bad")
    with bad.subscribe(seen.append):
        pass

    assert any(
        isinstance(msg, ErrorMessage)
        and msg.error.type_name == "ValueError"
        and msg.error.message == "boom"
        for msg in seen
    )


def test_derived_async_callback_is_rejected_at_registration():
    graph = Graph("py-async-error-smoke")
    source = graph.state(1, name="source")

    async def async_callback(_value: int) -> int:
        return 2

    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        graph.derived([source], async_callback, name="bad_async")


def test_batch_callback_exception_rolls_back_and_reraises_original_exception():
    graph = Graph("py-batch-smoke")
    source = graph.state(1, name="source")

    def mutate_then_raise() -> None:
        source.set(9)
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        graph.batch(mutate_then_raise)

    assert source.cache() == 1


def test_async_batch_callback_is_rejected_before_commit():
    graph = Graph("py-async-batch-smoke")
    source = graph.state(1, name="source")

    async def async_batch() -> None:
        source.set(9)

    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        graph.batch(async_batch)

    assert source.cache() == 1


def test_async_subscribe_callback_is_rejected_at_registration():
    graph = Graph("py-async-subscribe-smoke")
    source = graph.state(1, name="source")

    async def async_subscriber(_msg: Message[object]) -> None:
        pass

    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        source.subscribe(async_subscriber)


def test_none_is_valid_python_data_payload():
    seen: list[Message[object]] = []
    graph = Graph("py-none-smoke")
    source = graph.state(None, name="none_value")

    with source.subscribe(seen.append):
        pass

    assert source.cache() is None
    assert DataMessage(None) in seen
    node = next(node for node in graph.describe()["nodes"] if node["name"] == "none_value")
    assert node["has_value"] is True


def test_absent_cache_raises_without_conflating_cached_none():
    graph = Graph("py-no-data-smoke")
    source = graph.state(1, name="source")
    plus_one = graph.derived([source], lambda value: value + 1, name="plus_one")
    none_value = graph.state(None, name="none_value")

    assert plus_one.has_value is False
    with pytest.raises(GraphReflyNoDataError, match="no cached DATA"):
        plus_one.cache()
    assert plus_one.cache(default="missing") == "missing"

    assert none_value.has_value is True
    assert none_value.cache() is None


def test_advanced_node_ctx_emit_preserves_none_and_no_data_absence():
    graph = Graph("py-node-none-no-data-smoke")
    source = graph.state(1, name="source")
    none_node = graph.node([source], lambda ctx: ctx.emit(None), name="none_node")
    quiet_node = graph.node([source], lambda _ctx: None, name="quiet_node")

    with none_node.subscribe(lambda _msg: None), quiet_node.subscribe(lambda _msg: None):
        assert none_node.has_value is True
        assert none_node.cache() is None
        assert quiet_node.has_value is False
        with pytest.raises(GraphReflyNoDataError, match="no cached DATA"):
            quiet_node.cache()


def test_advanced_ctx_dep_presence_does_not_conflate_none_with_absence():
    graph = Graph("py-node-dep-presence-smoke")
    none_source = graph.state(None, name="none_source")
    trigger = graph.state(1, name="trigger")
    seen: list[tuple[bool, object, int]] = []

    def body(ctx: Ctx) -> None:
        seen.append((ctx.has_data(0), ctx.data(0, "missing"), ctx.data(1)))
        ctx.emit(seen[-1])

    node = graph.node([none_source, trigger], body, name="presence")
    with node.subscribe(lambda _msg: None):
        assert node.cache() == (True, None, 1)
        graph.invalidate(none_source)
        trigger.set(2)
        assert node.cache() == (False, "missing", 2)

    assert seen == [(True, None, 1), (False, "missing", 2)]


def test_advanced_node_decorator_form_uses_function_name_and_ctx():
    graph = Graph("py-node-decorator-smoke")
    source = graph.state(1, name="source")

    @graph.node([source])
    def plus_ten(ctx: Ctx) -> None:
        ctx.emit(ctx.data(0) + 10)

    with plus_ten.subscribe(lambda _msg: None):
        assert plus_ten.cache() == 11

    entry = next(node for node in graph.describe()["nodes"] if node["name"] == "plus_ten")
    assert entry["factory"] == "node"


def test_advanced_ctx_state_none_does_not_conflate_absence_when_has_state_is_checked():
    graph = Graph("py-node-state-none-smoke")
    source = graph.state(1, name="source")
    seen: list[tuple[bool, object | None]] = []

    def body(ctx: Ctx) -> None:
        seen.append((ctx.has_state, ctx.state))
        ctx.state = None
        ctx.emit(seen[-1])

    node = graph.node([source], body, name="state_none")
    with node.subscribe(lambda _msg: None):
        assert node.cache() == (False, None)
        source.set(2)
        assert node.cache() == (True, None)

    assert seen == [(False, None), (True, None)]


def test_advanced_node_async_callback_is_rejected_at_registration():
    graph = Graph("py-node-async-registration-smoke")
    source = graph.state(1, name="source")

    async def async_body(_ctx: Ctx) -> None:
        return None

    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        graph.node([source], async_body, name="async_node")

    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        @graph.node([source])
        async def async_decorated(_ctx: Ctx) -> None:
            return None


def test_advanced_ctx_multiple_emit_order_is_preserved():
    graph = Graph("py-node-multi-emit-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    def body(ctx: Ctx) -> None:
        ctx.emit(("first", ctx.data(0)))
        ctx.emit(("second", ctx.data(0)))

    node = graph.node([source], body, name="multi_emit")
    with node.subscribe(seen.append):
        assert node.cache() == ("second", 1)

    assert [msg.value for msg in seen if isinstance(msg, DataMessage)] == [
        ("first", 1),
        ("second", 1),
    ]


def test_advanced_ctx_index_out_of_range_raises_index_error():
    graph = Graph("py-node-index-error-smoke")
    source = graph.state(1, name="source")
    checked = False

    def body(ctx: Ctx) -> None:
        nonlocal checked
        with pytest.raises(IndexError):
            ctx.has_data(1)
        with pytest.raises(IndexError):
            ctx.data(1)
        checked = True
        ctx.emit(ctx.data(0))

    node = graph.node([source], body, name="index_error")
    with node.subscribe(lambda _msg: None):
        assert node.cache() == 1

    assert checked is True


def test_advanced_ctx_hook_async_callback_becomes_graph_error_without_leaking_coroutine(
    recwarn: pytest.WarningsRecorder,
):
    graph = Graph("py-node-async-hook-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    async def async_cleanup() -> None:
        return None

    def cleanup() -> object:
        return async_cleanup()

    def body(ctx: Ctx) -> None:
        ctx.on_invalidate(cleanup)
        ctx.emit(ctx.data(0))

    node = graph.node([source], body, name="async_hook")
    with node.subscribe(seen.append):
        seen.clear()
        graph.invalidate(source)

    gc.collect()
    assert any(
        isinstance(msg, ErrorMessage) and "async callbacks are deferred" in msg.error.message
        for msg in seen
    )
    assert not [
        warning
        for warning in recwarn
        if issubclass(warning.category, RuntimeWarning) and "never awaited" in str(warning.message)
    ]


def test_advanced_node_callback_exception_becomes_graph_error_observation():
    graph = Graph("py-node-error-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    def boom(_ctx: Ctx) -> None:
        raise ValueError("node boom")

    bad = graph.node([source], boom, name="bad")
    with bad.subscribe(seen.append):
        pass

    assert any(
        isinstance(msg, ErrorMessage)
        and msg.error.type_name == "ValueError"
        and msg.error.message == "node boom"
        for msg in seen
    )


def test_advanced_ctx_cleanup_hook_exception_becomes_graph_error():
    graph = Graph("py-node-hook-error-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    def flush() -> None:
        raise ValueError("hook boom")

    def body(ctx: Ctx) -> None:
        ctx.on_invalidate(flush)
        ctx.emit(ctx.data(0))

    node = graph.node([source], body, name="hook_error")
    with node.subscribe(seen.append):
        seen.clear()
        graph.invalidate(source)

    assert any(
        isinstance(msg, ErrorMessage)
        and msg.error.type_name == "ValueError"
        and msg.error.message == "hook boom"
        for msg in seen
    )


def test_advanced_ctx_commit_preserves_hook_order_before_emit_reentry():
    graph = Graph("py-node-hook-order-smoke")
    source = graph.state(1, name="source")
    events: list[str] = []
    subscription: list[Subscription] = []

    def cleanup() -> None:
        events.append("cleanup")

    def body(ctx: Ctx) -> None:
        ctx.on_deactivation(cleanup)
        ctx.emit(ctx.data(0))

    def observe(msg: Message[object]) -> None:
        if isinstance(msg, DataMessage) and msg.value == 2:
            events.append("data")
            subscription[0].unsubscribe()

    node = graph.node([source], body, name="hook_order")
    sub = node.subscribe(observe)
    subscription.append(sub)
    events.clear()

    source.set(2)

    assert events == ["data", "cleanup"]
    assert sub.closed is True


def test_advanced_ctx_is_inactive_during_commit_reentry():
    graph = Graph("py-node-ctx-scope-smoke")
    source = graph.state(1, name="source")
    stashed: list[Ctx] = []
    errors: list[GraphReflyRuntimeError] = []

    def body(ctx: Ctx) -> None:
        stashed.clear()
        stashed.append(ctx)
        ctx.emit(ctx.data(0))

    def observe(msg: Message[object]) -> None:
        if isinstance(msg, DataMessage) and msg.value == 2:
            with pytest.raises(GraphReflyRuntimeError) as exc_info:
                stashed[0].emit("late")
            errors.append(exc_info.value)

    node = graph.node([source], body, name="ctx_scope")
    with node.subscribe(observe):
        source.set(2)

    assert errors
    assert "ctx is only valid" in str(errors[0])


def test_advanced_node_fatal_base_exception_propagates_and_poisons_facade():
    graph = Graph("py-node-fatal-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    def boom(_ctx: Ctx) -> None:
        raise SystemExit("node exit")

    bad = graph.node([source], boom, name="bad")
    with pytest.raises(SystemExit, match="node exit"):
        bad.subscribe(seen.append)

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        bad.cache(default=None)
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_advanced_node_fatal_deactivation_hook_propagates_and_poisons_facade():
    graph = Graph("py-node-deactivation-fatal-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    def cleanup() -> None:
        raise SystemExit("deactivation exit")

    def body(ctx: Ctx) -> None:
        ctx.on_deactivation(cleanup)
        ctx.emit(ctx.data(0))

    node = graph.node([source], body, name="fatal_cleanup")
    sub = node.subscribe(seen.append)

    with pytest.raises(SystemExit, match="deactivation exit"):
        sub.unsubscribe()

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        node.cache(default=None)
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_advanced_node_fatal_during_batch_commit_propagates_without_graph_error():
    graph = Graph("py-node-batch-fatal-smoke")
    source = graph.state(0, name="source")
    seen: list[Message[object]] = []

    def boom(ctx: Ctx) -> None:
        if ctx.data(0) == 1:
            raise SystemExit("node batch exit")
        ctx.emit(ctx.data(0))

    bad = graph.node([source], boom, name="bad")
    with pytest.raises(SystemExit, match="node batch exit"), bad.subscribe(seen.append):
        graph.batch(lambda: source.set(1))

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        bad.cache(default=None)
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_rewire_next_fatal_callback_propagates_without_graph_error():
    graph = Graph("py-rewire-next-fatal-smoke")
    source = graph.state(0, name="source")
    helper = graph.state("helper", name="helper")
    seen: list[Message[object]] = []

    def body(ctx: Ctx) -> None:
        if ctx.dep_len > 1 and ctx.has_data(1):
            raise SystemExit("rewire next exit")
        if ctx.has_data(0) and ctx.data(0) == 1:
            ctx.rewire_next.subscribe_dep(helper, body)
        ctx.emit(ctx.data(0))

    node = graph.node([source], body, name="rewire-next-fatal", partial=True)
    with pytest.raises(SystemExit, match="rewire next exit"), node.subscribe(seen.append):
        source.set(1)

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        node.cache(default=None)
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_fatal_poison_preserves_original_exception_when_teardown_also_fails():
    graph = Graph("py-fatal-teardown-mask-smoke")
    source = graph.state(1, name="source")

    def cleanup() -> None:
        raise SystemExit("cleanup exit")

    def cleanup_body(ctx: Ctx) -> None:
        ctx.on_deactivation(cleanup)
        ctx.emit(ctx.data(0))

    cleanup_node = graph.node([source], cleanup_body, name="cleanup")
    cleanup_subscription = cleanup_node.subscribe(lambda _msg: None)

    def original(_ctx: Ctx) -> None:
        raise SystemExit("original exit")

    fatal_node = graph.node([source], original, name="fatal")
    with pytest.raises(SystemExit, match="original exit"):
        fatal_node.subscribe(lambda _msg: None)

    assert graph.closed is True
    assert cleanup_subscription.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        source.cache()


def test_decorators_and_context_manager_are_explicit_graph_owned_sugar():
    with Graph("py-decorator-smoke") as graph:
        source = graph.state(1, name="source")

        @graph.derived([source])
        def plus_one(value: int) -> int:
            return value + 1

        effects: list[int] = []

        @graph.effect([plus_one])
        def record(value: int) -> None:
            effects.append(value)

        with record.subscribe(lambda _msg: None):
            assert plus_one.cache() == 2
            source.set(4)
            assert plus_one.cache() == 5
            assert effects[-1] == 5

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="graph is closed"):
        plus_one.cache()


def test_subscriber_callback_errors_are_captured_at_python_boundary():
    graph = Graph("py-subscriber-error-smoke")
    source = graph.state(1, name="source")
    captured: list[SubscriberCallbackError] = []

    def subscriber(_msg: Message[int]) -> None:
        raise ValueError("observer boom")

    sub = source.subscribe(subscriber, on_error=captured.append)

    assert captured
    assert sub.callback_errors == tuple(captured)
    assert isinstance(captured[0].original, ValueError)
    assert captured[0].original.__traceback__ is None
    sub.unsubscribe()


def test_subscriber_fatal_base_exception_propagates_without_boundary_wrapping():
    graph = Graph("py-subscriber-fatal-smoke")
    source = graph.state(1, name="source")
    events: list[GraphEvent] = []

    def subscriber(_msg: Message[int]) -> None:
        raise SystemExit("exit")

    observer = graph.observe(events.append)
    with pytest.raises(SystemExit, match="exit"):
        source.subscribe(subscriber)

    assert graph.closed is True
    assert observer.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        source.cache()
    assert not any(isinstance(event.message, ErrorMessage) for event in events)


def test_subscriber_keyboard_interrupt_propagates_without_boundary_wrapping():
    graph = Graph("py-subscriber-keyboard-interrupt-smoke")
    source = graph.state(1, name="source")
    events: list[GraphEvent] = []

    def subscriber(_msg: Message[int]) -> None:
        raise KeyboardInterrupt

    observer = graph.observe(events.append)
    with pytest.raises(KeyboardInterrupt):
        source.subscribe(subscriber)

    assert graph.closed is True
    assert observer.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        source.cache()
    assert not any(isinstance(event.message, ErrorMessage) for event in events)


def test_subscriber_callback_errors_keep_bounded_history():
    graph = Graph("py-subscriber-error-history-smoke")
    source = graph.state(0, name="source")

    def subscriber(_msg: Message[int]) -> None:
        raise ValueError("observer boom")

    sub = source.subscribe(subscriber)
    for value in range(40):
        source.set(value)

    assert len(sub.callback_errors) == 32
    assert all(error.original.__traceback__ is None for error in sub.callback_errors)
    sub.unsubscribe()


def test_subscriber_on_error_failures_are_captured_at_python_boundary():
    graph = Graph("py-subscriber-on-error-smoke")
    source = graph.state(1, name="source")
    raised = False

    def subscriber(_msg: Message[int]) -> None:
        nonlocal raised
        if not raised:
            raised = True
            raise ValueError("observer boom")

    def on_error(_error: SubscriberCallbackError) -> None:
        raise RuntimeError("handler boom")

    sub = source.subscribe(subscriber, on_error=on_error)

    assert len(sub.callback_errors) == 2
    assert isinstance(sub.callback_errors[0].original, ValueError)
    assert isinstance(sub.callback_errors[1].original, RuntimeError)
    sub.unsubscribe()


def test_observe_callback_errors_are_captured_at_python_boundary():
    graph = Graph("py-observe-error-smoke")
    source = graph.state(1, name="source")
    captured: list[SubscriberCallbackError] = []

    def observer(event: GraphEvent) -> None:
        if event.message == DataMessage(2):
            raise ValueError("observe boom")

    sub = graph.observe(observer, on_error=captured.append)
    source.set(2)

    assert captured
    assert sub.callback_errors == tuple(captured)
    assert isinstance(captured[0].original, ValueError)
    assert captured[0].original.__traceback__ is None
    sub.unsubscribe()


def test_observe_fatal_base_exception_propagates_to_initiating_call():
    graph = Graph("py-observe-fatal-smoke")
    source = graph.state(1, name="source")

    def observer(event: GraphEvent) -> None:
        if event.message == DataMessage(2):
            raise SystemExit("observe exit")

    sub = graph.observe(observer)
    with pytest.raises(SystemExit, match="observe exit"):
        source.set(2)

    assert graph.closed is True
    assert sub.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        source.cache()


def test_observe_fatal_during_registration_propagates_without_graph_error():
    graph = Graph("py-observe-eager-fatal-smoke")
    source = graph.state(1, name="source")
    source.subscribe(lambda _msg: None)
    calls = 0

    def observer(_event: GraphEvent) -> None:
        nonlocal calls
        calls += 1
        raise SystemExit("observe eager exit")

    with pytest.raises(SystemExit, match="observe eager exit"):
        graph.observe(observer)

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        source.cache()
    assert calls == 1


def test_graph_observe_uses_typed_message_shape():
    seen: list[GraphEvent] = []
    graph = Graph("py-observe-shape-smoke")
    source = graph.state(1, name="source")
    graph.derived([source], lambda value: value + 1, name="plus_one")

    with graph.observe(seen.append):
        source.set(4)

    assert any(
        event.path.endswith("plus_one")
        and event.message == DataMessage(5)
        and event.tier == 3
        for event in seen
    )


def test_describe_exposes_factory_metadata_without_raw_function_bodies():
    graph = Graph("py-describe-smoke")
    source = graph.state(1, name="source")
    plus_one = graph.derived([source], lambda value: value + 1, name="plus_one")

    sub = plus_one.subscribe(lambda _msg: None)
    snapshot = graph.describe()
    sub.unsubscribe()
    nodes = {node["name"]: node for node in snapshot["nodes"]}

    assert nodes["source"]["factory"] == "state"
    assert nodes["plus_one"]["factory"] == "derived"
    assert nodes["plus_one"]["has_value"] is True
    assert "lambda" not in repr(snapshot)


def test_restore_public_surface_has_no_storage_or_hidden_runtime_hydration():
    checkpoint_params = signature(Graph.checkpoint).parameters
    restore_params = signature(restore_graph).parameters
    registry_params = signature(restore_registry).parameters
    ref_params = signature(restore_ref).parameters

    assert list(checkpoint_params) == ["self"]
    assert list(restore_params) == ["checkpoint", "registry"]
    assert restore_params["registry"].kind is restore_params["registry"].KEYWORD_ONLY
    assert "storage" not in restore_params
    assert "runner" not in restore_params
    assert "hydrate" not in restore_params
    assert "into" not in restore_params
    assert list(registry_params) == ["entries", "include_builtins"]
    assert (
        registry_params["include_builtins"].kind
        is registry_params["include_builtins"].KEYWORD_ONLY
    )
    assert list(ref_params) == ["ref", "config", "config_version"]
    assert ref_params["config"].kind is ref_params["config"].KEYWORD_ONLY
    assert ref_params["config_version"].kind is ref_params["config_version"].KEYWORD_ONLY

    public_registry = restore_registry([])
    native = import_module("graphrefly._native")
    assert not isinstance(public_registry, native.RestoreRegistry)
    with pytest.raises(GraphReflyRestoreError, match="graphrefly.restore_registry"):
        restore_graph({}, registry=native.restore_registry([], True))
    assert not hasattr(graphrefly, "NativeGraph")
    assert not hasattr(graphrefly, "NativeNode")
    assert not hasattr(graphrefly, "RestoreNativeContext")


def test_public_value_and_runtime_errors_are_facade_exceptions():
    graph = Graph("py-error-boundary-smoke")

    graph.state(1, name="same")
    with pytest.raises(GraphReflyRuntimeError, match="duplicate graph node id"):
        graph.state(2, name="same")

    source = graph.state(1, name="source")
    derived = graph.derived([source], lambda value: value + 1, name="derived")
    with pytest.raises(GraphReflyRuntimeError, match="state nodes"):
        derived.set(3)


def test_public_control_facade_is_graph_owned_and_validates_lock_id():
    graph = Graph("py-control-facade-smoke")
    other_graph = Graph("py-control-other-graph")
    source = graph.state(1, name="source")
    other_source = other_graph.state(1, name="other_source")
    derived = graph.derived([source], lambda value: value + 1, name="derived")
    seen: list[Message[int]] = []

    with derived.subscribe(seen.append):
        assert derived.cache() == 2
        graph.pause(derived, "lock")
        source.set(2)
        assert derived.cache() == 2
        graph.resume(derived, "lock")
        assert derived.cache() == 3
        seen.clear()
        graph.invalidate(derived)

    assert ControlMessage("INVALIDATE") in seen
    with pytest.raises(GraphReflyRuntimeError, match="must belong"):
        graph.invalidate(other_source)
    with pytest.raises(GraphReflyValueError, match="lock_id must be a str"):
        graph.pause(derived, object())  # type: ignore[arg-type]


def test_graph_callback_fatal_base_exception_propagates_without_graph_error():
    graph = Graph("py-graph-fatal-smoke")
    source = graph.state(1, name="source")

    def boom(_value: int) -> int:
        raise SystemExit("graph exit")

    bad = graph.derived([source], boom, name="bad")
    with pytest.raises(SystemExit, match="graph exit"):
        bad.subscribe(lambda _msg: None)

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        bad.cache(default=None)


def test_graph_callback_keyboard_interrupt_propagates_without_graph_error():
    graph = Graph("py-graph-keyboard-interrupt-smoke")
    source = graph.state(1, name="source")

    def boom(_value: int) -> int:
        raise KeyboardInterrupt

    bad = graph.derived([source], boom, name="bad")
    with pytest.raises(KeyboardInterrupt):
        bad.subscribe(lambda _msg: None)

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        bad.cache(default=None)


def test_graph_callback_fatal_during_batch_commit_propagates_without_graph_error():
    graph = Graph("py-batch-commit-fatal-smoke")
    source = graph.state(0, name="source")
    seen: list[Message[object]] = []

    def boom(value: int) -> int:
        if value == 1:
            raise SystemExit("batch commit exit")
        return value

    bad = graph.derived([source], boom, name="bad")
    with pytest.raises(SystemExit, match="batch commit exit"), bad.subscribe(seen.append):
        graph.batch(lambda: source.set(1))

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        bad.cache(default=None)
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_graph_callback_keyboard_interrupt_during_batch_commit_stays_fatal():
    graph = Graph("py-batch-commit-keyboard-interrupt-smoke")
    source = graph.state(0, name="source")
    seen: list[Message[object]] = []

    def boom(value: int) -> int:
        if value == 1:
            raise KeyboardInterrupt
        return value

    bad = graph.derived([source], boom, name="bad")
    with pytest.raises(KeyboardInterrupt), bad.subscribe(seen.append):
        graph.batch(lambda: source.set(1))

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        bad.cache(default=None)
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_graph_close_releases_facade_subscriptions_and_rejects_later_use():
    graph = Graph("py-close-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[int]] = []
    sub = source.subscribe(seen.append)

    assert sub.closed is False
    graph.close()
    graph.close()

    assert graph.closed is True
    assert sub.closed is True
    sub.unsubscribe()
    with pytest.raises(GraphReflyRuntimeError, match="graph is closed"):
        source.cache()
    with pytest.raises(GraphReflyRuntimeError, match="graph is closed"):
        graph.state(2, name="after_close")


def test_graph_retain_keeps_node_active_after_returned_handle_is_dropped():
    graph = Graph("py-retain-drop-smoke")
    source = graph.state(1, name="source")
    runs: list[int] = []

    def double(value: int) -> int:
        runs.append(value)
        return value * 2

    retained = graph.derived([source], double, name="retained")
    graph.retain(retained, reason="test.keepalive")
    gc.collect()

    assert retained.cache() == 2
    source.set(2)

    assert runs == [1, 2]
    assert retained.cache() == 4
    graph.close()


def test_graph_retain_release_is_idempotent_and_stops_activation_root():
    graph = Graph("py-retain-release-smoke")
    source = graph.state(1, name="source")
    runs: list[int] = []

    retained = graph.derived(
        [source],
        lambda value: runs.append(value) or value,
        name="retained",
    )
    keepalive = graph.retain(retained)

    assert runs == [1]
    keepalive.release()
    keepalive.release()
    source.set(2)

    assert keepalive.closed is True
    assert runs == [1]


def test_graph_retain_close_releases_handle_and_rejects_cross_graph_nodes():
    graph = Graph("py-retain-close-smoke")
    other = Graph("py-retain-other")
    source = graph.state(1, name="source")
    retained = graph.derived([source], lambda value: value, name="retained")
    keepalive = graph.retain(retained)

    assert keepalive.closed is False
    with pytest.raises(GraphReflyRuntimeError, match="node must belong"):
        other.retain(retained)
    graph.close()

    assert keepalive.closed is True
    other.close()


def test_graph_retain_is_release_token_not_observer_subscription():
    graph = Graph("py-retain-shape-smoke")
    node = graph.state(1, name="source")
    keepalive = graph.retain(node)

    assert isinstance(keepalive, Retain)
    assert not isinstance(keepalive, Subscription)
    assert not hasattr(keepalive, "unsubscribe")
    assert not hasattr(keepalive, "callback_errors")

    keepalive.release()


def test_graph_retain_context_manager_releases_activation_root():
    graph = Graph("py-retain-context-smoke")
    source = graph.state(1, name="source")
    runs: list[int] = []
    retained = graph.derived(
        [source],
        lambda value: runs.append(value) or value,
        name="retained",
    )

    with graph.retain(retained) as keepalive:
        assert keepalive.closed is False
        assert runs == [1]

    assert keepalive.closed is True
    source.set(2)
    assert runs == [1]


def test_graph_retain_rejects_non_string_reason():
    graph = Graph("py-retain-reason-smoke")
    node = graph.state(1, name="source")

    with pytest.raises(GraphReflyValueError, match="retain reason"):
        graph.retain(node, reason=object())  # type: ignore[arg-type]


def test_d557_graph_close_uses_reverse_graph_owned_resource_stack():
    graph = Graph("py-d557-resource-stack")
    source = graph.state(1, name="source")
    retained = graph.derived([source], lambda value: value, name="retained")
    keepalive = graph.retain(retained)
    bridge = graphrefly.wire_bridge(graph, session_id="s1", name="bridge")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge, name="protobuf")
    group = graphrefly.wire_edge_group(graph, bridge, inbound_edges=["edge-a"], name="group")
    ack = graphrefly.wire_bridge_ack_driver(
        graph,
        bridge,
        clock=graph.state(0, name="clock"),
        timeout_ms=5,
        name="ack",
    )
    subscription = source.subscribe(lambda _msg: None)
    order: list[str] = []

    def wrap_close(resource: object, label: str) -> None:
        original = resource._close_from_graph  # type: ignore[attr-defined]

        def close() -> None:
            order.append(label)
            original()

        resource._close_from_graph = close  # type: ignore[attr-defined]

    wrap_close(keepalive, "retain")
    wrap_close(bridge, "bridge")
    wrap_close(protobuf, "protobuf")
    wrap_close(group, "group")
    wrap_close(ack, "ack")
    wrap_close(subscription, "subscription")
    wrap_close(keepalive._subscription, "retain-subscription")  # noqa: SLF001

    graph.close()

    assert order == [
        "subscription",
        "ack",
        "group",
        "protobuf",
        "bridge",
        "retain",
        "retain-subscription",
    ]
    assert keepalive.closed is True
    assert subscription.closed is True


def test_d557_resource_stack_continues_after_resource_close_error():
    graph = Graph("py-d557-resource-stack-error")
    order: list[str] = []

    class Resource:
        def __init__(self, label: str, *, raises: bool = False) -> None:
            self.label = label
            self.raises = raises

        def _close_from_graph(self) -> None:
            order.append(self.label)
            if self.raises:
                raise RuntimeError(self.label)

    first = Resource("first")
    second = Resource("second")
    third = Resource("third", raises=True)
    graph._lifetime.register_resource(first)  # noqa: SLF001
    graph._lifetime.register_resource(second)  # noqa: SLF001
    graph._lifetime.register_resource(third)  # noqa: SLF001

    with pytest.raises(RuntimeError, match="third"):
        graph.close()

    assert order == ["third", "second", "first"]


def test_d557_graph_close_closes_public_resource_subscriptions_before_resource_stack():
    graph = Graph("py-d557-resource-close-with-public-subscription")
    bridge = graphrefly.wire_bridge(graph, session_id="s1")
    protobuf = graphrefly.wire_bridge_protobuf(graph, bridge)
    clock = graph.state(1000, name="clock")
    ack = graphrefly.wire_bridge_ack_driver(graph, bridge, clock=clock, timeout_ms=5)
    source = graph.state(b"first", name="source")
    graphrefly.wire_edge_group(graph, bridge, outbound_edges={"edge-a": source})
    timeouts: list[graphrefly.WireBridgeAckTimeout] = []
    outbound: list[bytes] = []

    with (
        ack.timeouts.subscribe(_record_data(timeouts)),
        protobuf.outbound_bytes.subscribe(_record_data(outbound)),
    ):
        assert len(outbound) == 1
        graph.close()

    assert graph.closed is True
    assert ack._released is True  # noqa: SLF001
    assert bridge._released is True  # noqa: SLF001


def test_dropped_subscription_handle_releases_native_subscription():
    graph = Graph("py-subscription-drop-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[int]] = []

    sub = source.subscribe(seen.append)
    assert DataMessage(1) in seen
    del sub
    gc.collect()

    source.set(2)

    assert DataMessage(2) not in seen


def test_callback_error_class_is_public_taxonomy_for_future_mapping():
    assert issubclass(CallbackError, graphrefly.GraphReflyError)
