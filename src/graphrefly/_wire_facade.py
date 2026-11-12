"""Public wire bridge facades over the Python kernel (graphrefly-py:D8, graphrefly:D803).

Wire edge groups (R-bridge) carry a fixed set of edges from one graph to another:
:func:`wire_edge_group_out` turns a sender wave into one :class:`WireEdgeCause` frame,
:func:`frame_sink` hands frames to a transport, :func:`frame_source` turns a
transport's receiving end into a node, and :func:`wire_edge_group_in` releases a
session's causes in order, at most once, as one receiver wave each.

:func:`wire_bridge` is the transport adapter over canonical protobuf bytes: numbered
DATA envelopes, acks, retries on explicit ack-timeout facts, and issues that are never
terminals; :func:`wire_bridge_ack_driver` derives those facts from a clock node. No
timer is set anywhere.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from threading import get_ident
from typing import Any

from graphrefly import _kernel as _k
from graphrefly._facade import (
    Array,
    Graph,
    Node,
    State,
    _guard_fn,
    _reject_async_callable,
)
from graphrefly._wire import bridge as _bridge
from graphrefly._wire import envelope as _envelope
from graphrefly._wire.bridge import WireEdgeGroupIssue
from graphrefly._wire.envelope import (
    WireBridgeAckDriverIssue,
    WireBridgeAckDriverStatus,
    WireBridgeAckTimeout,
    WireBridgeEvent,
    WireBridgeIndexedEvent,
    WireBridgeIssue,
    WireBridgeStatus,
)
from graphrefly._wire.protobuf import WireEdgeArrays, WireEdgeCause
from graphrefly.exceptions import GraphReflyRuntimeError, GraphReflyValueError

__all__ = [
    "WireBridge",
    "WireBridgeAckDriver",
    "WireBridgeAckDriverIssue",
    "WireBridgeAckDriverStatus",
    "WireBridgeAckTimeout",
    "WireBridgeIssue",
    "WireBridgeEvent",
    "WireBridgeIndexedEvent",
    "WireBridgeStatus",
    "WireEdgeArrays",
    "WireEdgeCause",
    "WireEdgeGroupIn",
    "WireEdgeGroupIssue",
    "frame_sink",
    "frame_source",
    "wire_bridge",
    "wire_bridge_ack_driver",
    "wire_edge_group_in",
    "wire_edge_group_out",
]


def _prefix(graph: Graph, name: str | None, kind: str) -> str:
    if name is None:
        graph._next_id += 1
        return f"#{kind}{graph._next_id}"
    return graph._new_id(name)


def _adopt(graph: Graph, node_id: str, *, state: bool = False) -> Node[Any]:
    handle: Node[Any] = State(graph, node_id) if state else Node(graph, node_id)
    graph._register(handle)
    return handle


def _build(graph: Graph, build: Callable[[], Any]) -> Any:
    graph._check_thread()
    try:
        return graph._call(build)
    except ValueError as error:
        raise GraphReflyValueError(str(error)) from error


def wire_edge_group_out(
    graph: Graph,
    *,
    group: str,
    edges: Mapping[str, Node[bytes]],
    name: str | None = None,
) -> Node[WireEdgeCause]:
    """Build the sender side of a wire edge group: a node whose values are its frames.

    It depends on ``edges`` (edge id to node) under manual policies; each wave in which
    an edge sent is one cause, one :class:`WireEdgeCause` carrying only the edges that
    sent. A session is one lifetime of this node, and its first cause carries every
    edge's snapshot. DATA values and ERROR payloads cross as bytes; anything else is a
    throw, so this node's ERROR.
    """

    if not isinstance(group, str) or not group:
        msg = "group must be a non-empty str"
        raise GraphReflyValueError(msg)
    ids = {edge: graph._node_id(node) for edge, node in edges.items()}
    node_id = _prefix(graph, name, "out")
    _build(
        graph,
        lambda: _bridge.wire_edge_group_out(
            graph._kernel, node_id, group=group, edges=ids, guard=_guard_fn
        ),
    )
    return _adopt(graph, node_id)


def frame_sink(send: Callable[[Any], object]) -> Callable[[Array[Any]], None]:
    """A subscribe callback that hands each value a subscription delivers to ``send``:
    frames from a wire edge group's outbound node, or bytes from a bridge.

    The START snapshot's frame is included; a frame delivered twice is dropped by the
    receiving gate as a cause already taken.
    """

    _reject_async_callable(send)

    def sink(array: Array[Any]) -> None:
        for message in array:
            if message[0] == _k.DATA:
                send(message[1])

    return sink


def frame_source(
    graph: Graph,
    open: Callable[[Callable[[object], None]], Callable[[], object] | None],
    *,
    name: str | None = None,
) -> Node[Any]:
    """The receiving end of a transport, as a node with no dependencies.

    At each activation it calls ``open(deliver)``, and what that returns is called when
    the lifetime closes. Each ``deliver(frame)`` on the graph's owner thread is one wave;
    a transport on another thread re-enters through ``Graph.reentry_queue``.
    """

    _reject_async_callable(open)
    owner = graph._owner_thread
    node_id = _prefix(graph, name, "frames")

    def kernel_open(deliver: Callable[[object], None]) -> Callable[[], object] | None:
        def checked(frame: object) -> None:
            if get_ident() != owner:
                msg = "frame_source deliver must run on the graph owner thread"
                raise GraphReflyRuntimeError(msg)
            graph._call(lambda: deliver(frame))

        close = open(checked)
        if close is not None and not callable(close):
            msg = "frame_source open returns a close function or None"
            raise GraphReflyValueError(msg)
        return close

    _build(graph, lambda: _bridge.frame_source(graph._kernel, node_id, kernel_open, _guard_fn))
    return _adopt(graph, node_id)


@dataclass(frozen=True, slots=True)
class WireEdgeGroupIn:
    """The receiver side of a wire edge group: its gate, one inbound node per edge, and
    its issues node (drops, gaps and refusals, as values)."""

    gate: Node[Any]
    inbound: Mapping[str, Node[bytes]]
    issues: Node[WireEdgeGroupIssue]


def wire_edge_group_in(
    graph: Graph,
    *,
    group: str,
    frames: Node[Any],
    edges: Iterable[str],
    name: str | None = None,
) -> WireEdgeGroupIn:
    """Build the receiver side of a wire edge group over ``frames``.

    The gate takes the frames bearing ``group``'s name and releases a session's causes
    in order and at most once; a release is one wave in which each carried edge's
    inbound node sends that edge's arrays. Stale causes are dropped, a gap is released
    and reported, and a frame with an undeclared edge or arrays an inbound node could
    not send is refused whole; each is a value of ``issues``, never a terminal.
    """

    if not isinstance(group, str) or not group:
        msg = "group must be a non-empty str"
        raise GraphReflyValueError(msg)
    declared = list(edges)
    if not declared or not all(isinstance(e, str) and e for e in declared):
        msg = "edges must be a non-empty list of edge id strings"
        raise GraphReflyValueError(msg)
    frames_id = graph._node_id(frames)
    node_id = _prefix(graph, name, "in")
    ids = _build(
        graph,
        lambda: _bridge.wire_edge_group_in(
            graph._kernel, node_id, group=group, frames=frames_id, edges=declared, guard=_guard_fn
        ),
    )
    return WireEdgeGroupIn(
        _adopt(graph, ids.gate),
        {edge: _adopt(graph, inbound) for edge, inbound in ids.inbound.items()},
        _adopt(graph, ids.issues),
    )


class WireBridge:
    """A wire bridge: the envelope layer between frames and canonical protobuf bytes.

    ``events`` sends ``{index, event}`` facts with a monotonic index. The bridge is
    retained until ``release()`` or graph close, even without external subscribers.
    ``outbound_bytes`` sends canonical ``WireBridgeEnvelope`` bytes to the transport;
    ``frames`` and ``values`` are what arrived; ``ack_timeouts`` is the explicit ingress
    of ack-timeout facts; ``status`` and ``issues`` are graph-visible facts. ``release()``
    is idempotent host lifetime cleanup: the bridge and its drivers stop acting.
    """

    def __init__(
        self, graph: Graph, nodes: _envelope.BridgeNodes, switch: _envelope.Released
    ) -> None:
        self._graph = graph
        self._switch = switch
        self._drivers: list[WireBridgeAckDriver] = []
        self._nodes = nodes
        self.events: Node[WireBridgeIndexedEvent] = _adopt(graph, nodes.events)
        _adopt(graph, nodes.status_ledger, state=True)
        self.outbound_bytes: Node[bytes] = _adopt(graph, nodes.outbound_bytes)
        self.frames: Node[WireEdgeCause] = _adopt(graph, nodes.frames)
        self.values: Node[bytes] = _adopt(graph, nodes.values)
        self.status: Node[WireBridgeStatus] = _adopt(graph, nodes.status)
        self.issues: Node[WireBridgeIssue] = _adopt(graph, nodes.issues)
        self.ack_timeouts: State[WireBridgeAckTimeout] = _adopt(
            graph, nodes.ack_timeouts, state=True
        )  # type: ignore[assignment]
        core = _adopt(graph, nodes.core)
        self._stops = [graph.retain(core), graph.retain(self.events)]

    @property
    def released(self) -> bool:
        """Return whether this bridge has been released."""

        return self._switch.released

    def release(self) -> None:
        """Stop the bridge and its ack drivers; later facts change nothing."""

        self._graph._check_thread(allow_closed=True)
        for driver in reversed(self._drivers):
            driver.release()
        self._switch.released = True
        for stop in reversed(self._stops):
            stop()

    def _close_from_graph(self) -> None:
        self.release()


def wire_bridge(
    graph: Graph,
    *,
    session_id: str,
    outbound: Iterable[Node[Any]] = (),
    received: Node[bytes] | None = None,
    max_attempts: int = 2,
    name: str | None = None,
) -> WireBridge:
    """Build a wire bridge over ``outbound`` nodes and a ``received`` byte source.

    Each frame (or bytes value) an outbound node sends becomes a numbered DATA envelope,
    pending until the peer acks it; an ack-timeout fact for its pending attempt sends it
    again with the next attempt until ``max_attempts``, when it is exhausted and
    reported. Each received DATA envelope is acked and its frame or value comes out of
    ``frames`` or ``values``. Malformed bytes and facts are issues, not terminals.
    """

    out_ids = [graph._node_id(node) for node in outbound]
    received_id = None if received is None else graph._node_id(received)
    node_id = _prefix(graph, name, "bridge")
    switch = _envelope.Released()
    nodes = _build(
        graph,
        lambda: _envelope.wire_bridge(
            graph._kernel,
            node_id,
            session_id=session_id,
            outbound=out_ids,
            received=received_id,
            max_attempts=max_attempts,
            released=switch,
            guard=_guard_fn,
        ),
    )
    bridge = WireBridge(graph, nodes, switch)
    graph._lifetime.register_resource(bridge)
    return bridge


class WireBridgeAckDriver:
    """Derives ack-timeout facts from a clock node and writes them to its bridge.

    ``timeouts`` shows each fact it derived, ``status`` its last one, ``issues`` clock
    facts that were not non-decreasing ints. ``release()`` stops it.
    """

    def __init__(
        self, graph: Graph, nodes: _envelope.DriverNodes, switch: _envelope.Released
    ) -> None:
        self._graph = graph
        self._graph = graph
        self._switch = switch
        self.timeouts: Node[WireBridgeAckTimeout] = _adopt(graph, nodes.timeouts)
        self.status: Node[WireBridgeAckDriverStatus] = _adopt(graph, nodes.status)
        self.issues: Node[WireBridgeAckDriverIssue] = _adopt(graph, nodes.issues)
        self._stop = graph.retain(_adopt(graph, nodes.core))

    @property
    def released(self) -> bool:
        """Return whether this driver has been released."""

        return self._switch.released

    def release(self) -> None:
        """Stop deriving timeout facts; idempotent."""

        self._graph._check_thread(allow_closed=True)
        self._switch.released = True
        self._stop()

    def _close_from_graph(self) -> None:
        self.release()


def wire_bridge_ack_driver(
    graph: Graph,
    bridge: WireBridge,
    *,
    clock: Node[int],
    timeout_ms: int,
    name: str | None = None,
) -> WireBridgeAckDriver:
    """Derive ack-timeout facts for ``bridge`` from ``clock``, a node of non-decreasing
    millisecond ints the host writes: an attempt pending for ``timeout_ms`` or more
    times out once. A clock fact that is not such an int is an issue."""

    if not isinstance(bridge, WireBridge) or bridge._graph is not graph:
        msg = "bridge must be a wire_bridge of this graph"
        raise GraphReflyRuntimeError(msg)
    if bridge.released:
        msg = "the bridge has been released"
        raise GraphReflyRuntimeError(msg)
    clock_id = graph._node_id(clock)
    node_id = _prefix(graph, name, "ack")
    switch = _envelope.Released()
    nodes = _build(
        graph,
        lambda: _envelope.wire_bridge_ack_driver(
            graph._kernel,
            node_id,
            bridge._nodes,
            clock=clock_id,
            timeout_ms=timeout_ms,
            released=switch,
            guard=_guard_fn,
        ),
    )
    driver = WireBridgeAckDriver(graph, nodes, switch)
    bridge._drivers.append(driver)
    return driver
