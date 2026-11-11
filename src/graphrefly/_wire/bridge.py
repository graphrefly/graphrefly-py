"""Wire edge groups over the Python kernel (R-bridge, graphrefly:D803).

A group carries a fixed set of edges from a sender graph to a receiver graph.

- Sender: the group's outbound node depends on the group's edges, under manual
  COMPLETE, ERROR and INVALIDATE policies, so it runs at most once per wave on a
  consistent set of inputs, and each run in which some edge brought input is one
  cause, sent as one DATA value: a :class:`WireEdgeCause` frame. A sink hands frames
  to the transport, which is not the bridge's.
- Receiver: frames come out of a node with no dependencies whose setup opens the
  receiving end, so each frame is one wave. The gate checks it, and every inbound node
  depends on the gate, so the edges of a cause settle in that one wave and a fan-in
  over them runs once.

Names follow the reference module in Python casing: ``wire_edge_group_out``,
``wire_edge_group_in``, ``frame_source``, ``frame_sink``. Bridge nodes carry no recipe,
so a checkpoint treats them as local-only (R-bridge does not cover checkpoints).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from graphrefly._kernel import COMPLETE, DATA, ERROR, INVALIDATE, Ctx, Dep, Graph, Message
from graphrefly._wire.protobuf import WireEdgeArrays, WireEdgeCause

_CROSSING = frozenset({DATA, INVALIDATE, COMPLETE, ERROR})

type Guard = Callable[[Callable[[Ctx], None]], Callable[[Ctx], None]]


def _same(fn: Callable[[Ctx], None]) -> Callable[[Ctx], None]:
    return fn


@dataclass(frozen=True, slots=True)
class WireEdgeGroupIssue:
    """A drop, gap, refusal or malformed frame at a group's gate (R-bridge).

    ``kind`` is ``stale`` (a cause at or below the last one taken; ``last`` names it),
    ``gap`` (causes ``missing_from`` to ``missing_to`` never came), ``refused`` (the
    frame was refused whole; ``reason`` says why) or ``malformed``.
    """

    kind: Literal["stale", "gap", "refused", "malformed"]
    session: str | None = None
    cause: int | None = None
    last: int | None = None
    missing_from: int | None = None
    missing_to: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _EdgeView:
    values: tuple[bytes, ...] | None
    terminal: Message | None


@dataclass(frozen=True, slots=True)
class _GateOutput:
    released: tuple[WireEdgeCause, ...]
    issues: tuple[WireEdgeGroupIssue, ...]
    view: Mapping[str, _EdgeView]


@dataclass(slots=True)
class _GateState:
    session: str | None = None
    last: int = 0
    view: dict[str, _EdgeView] = field(default_factory=dict)


@dataclass(slots=True)
class _OutState:
    session: str
    cause: int = 0


def _as_bytes(value: object) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    return None


def _brought(dep: Dep) -> list[tuple[Message, ...]]:
    """What one input brought in this run, as the outbound ctx gives it: an INVALIDATE,
    then the arrays that carried DATA, in order, then its terminal."""

    arrays: list[tuple[Message, ...]] = []
    if dep.invalidated:
        arrays.append(((INVALIDATE,),))
    for wave in dep.waves:
        if wave:
            arrays.append(tuple((DATA, value) for value in wave))
    if dep.terminal is not None:
        arrays.append((dep.terminal,))
    return arrays


def _bytes_only(
    node_id: str, edge: str, arrays: list[tuple[Message, ...]]
) -> tuple[tuple[Message, ...], ...]:
    out: list[tuple[Message, ...]] = []
    for array in arrays:
        messages: list[Message] = []
        for message in array:
            if message[0] in (DATA, ERROR):
                payload = _as_bytes(message[1])
                if payload is None:
                    msg = (
                        f"{node_id}: edge {edge} sent a {message[0]} that is not bytes;"
                        " encode it before the edge"
                    )
                    raise TypeError(msg)
                messages.append((message[0], payload))
            else:
                messages.append(message)
        out.append(tuple(messages))
    return tuple(out)


def wire_edge_group_out(
    g: Graph, node_id: str, *, group: str, edges: Mapping[str, str], guard: Guard = _same
) -> str:
    """Build the sender side of a group: an outbound node over ``edges`` (edge id to
    node id) whose DATA values are :class:`WireEdgeCause` frames, one per cause.

    A session is one lifetime of this node: its setup names it, and its causes count
    from 1. The first run brings every edge's snapshot, and an INVALIDATE for an edge
    without one. A DATA value or ERROR payload that is not bytes is a throw, so the
    node's ERROR.
    """

    edge_ids = list(edges)

    def setup(ctx: Ctx) -> None:
        ctx.state = _OutState(uuid.uuid4().hex)

    def run(ctx: Ctx) -> None:
        state = ctx.state
        assert isinstance(state, _OutState)
        first = state.cause == 0
        carried: list[WireEdgeArrays] = []
        for edge, dep in zip(edge_ids, ctx.deps, strict=True):
            arrays = _brought(dep)
            if arrays:
                carried.append(WireEdgeArrays(edge, _bytes_only(node_id, edge, arrays)))
            elif first:
                carried.append(WireEdgeArrays(edge, (((INVALIDATE,),),)))
        if not carried:
            return  # a run with no new input forms no cause
        state.cause += 1
        frame = WireEdgeCause(group, state.session, state.cause, tuple(carried))
        ctx.down([(DATA, frame)])

    g.node(
        node_id,
        [edges[e] for e in edge_ids],
        guard(run),
        complete="manual",
        error="manual",
        invalidate="manual",
        setup=setup,
    )
    return node_id


def frame_sink(send: Callable[[object], object]) -> Callable[[tuple[Message, ...]], None]:
    """A kernel sink callback that hands every frame an array carries to ``send``, the
    START snapshot's included. A frame delivered twice is dropped by the receiver."""

    def sink(array: tuple[Message, ...]) -> None:
        for message in array:
            if message[0] == DATA:
                send(message[1])

    return sink


def frame_source(
    g: Graph,
    node_id: str,
    open_: Callable[[Callable[[object], None]], Callable[[], object] | None],
    guard: Guard = _same,
) -> str:
    """The receiving end: a node with no dependencies whose setup calls
    ``open_(deliver)`` and returns what it returns as the lifetime's cleanup. Each
    ``deliver(frame)`` is a late output, so one wave."""

    def setup(ctx: Ctx) -> Callable[[], object] | None:
        return open_(lambda frame: ctx.down([(DATA, frame)]))

    g.node(node_id, [], guard(lambda ctx: None), setup=setup)
    return node_id


@dataclass(frozen=True, slots=True)
class InboundGroup:
    """The receiver side's node ids: the gate, one inbound node per edge, the issues."""

    gate: str
    inbound: Mapping[str, str]
    issues: str


def wire_edge_group_in(
    g: Graph,
    node_id: str,
    *,
    group: str,
    frames: str,
    edges: Sequence[str],
    guard: Guard = _same,
) -> InboundGroup:
    """Build the receiver side: the gate ``node_id`` over ``frames``, an inbound node
    ``{node_id}/{edge}`` per declared edge and the issues node ``{node_id}.issues``."""

    declared = list(edges)
    if len(set(declared)) != len(declared):
        msg = f"{node_id}: edge ids must be distinct"
        raise ValueError(msg)
    g.node(
        node_id,
        [frames],
        guard(_gate_fn(group, frozenset(declared))),
        complete="manual",
        error="manual",
    )
    inbound = {edge: f"{node_id}/{edge}" for edge in declared}
    for edge, inbound_id in inbound.items():
        g.node(inbound_id, [node_id], guard(_inbound_fn(edge)), complete="manual", error="manual")
    issues = f"{node_id}.issues"
    g.node(issues, [node_id], guard(_issues_fn), complete="manual", error="manual")
    return InboundGroup(node_id, inbound, issues)


class _BadFrame(Exception):
    pass


def _view_after(
    frame: WireEdgeCause, known: frozenset[str], view: Mapping[str, _EdgeView]
) -> dict[str, _EdgeView]:
    """The view after ``frame``, or _BadFrame with the reason it is refused whole."""

    after = dict(view)
    seen: set[str] = set()
    for entry in frame.edges:
        if not isinstance(entry, WireEdgeArrays):
            raise _BadFrame("an edge entry is not WireEdgeArrays")
        edge = entry.edge_id
        if edge not in known:
            raise _BadFrame(f"undeclared edge {edge}")
        if edge in seen:
            raise _BadFrame(f"edge {edge} twice")
        seen.add(edge)
        if not entry.arrays:
            raise _BadFrame(f"edge {edge} carries no array")
        prior = after.get(edge, _EdgeView(None, None))
        values, terminal = prior.values, prior.terminal
        for array in entry.arrays:
            if not array:
                raise _BadFrame(f"edge {edge}: an empty array")
            if terminal is not None:
                raise _BadFrame(f"edge {edge}: an array after its terminal")
            for index, message in enumerate(array):
                kind = message[0] if isinstance(message, tuple) and message else None
                if kind not in _CROSSING:
                    raise _BadFrame(f"edge {edge}: {kind} does not cross")
                if kind in (DATA, ERROR) and (
                    len(message) != 2 or not isinstance(message[1], bytes)
                ):
                    raise _BadFrame(f"edge {edge}: {kind} without bytes")
                if kind in (COMPLETE, ERROR) and index != len(array) - 1:
                    raise _BadFrame(f"edge {edge}: a terminal not last")
            # As the kernel applies it: what came before the last INVALIDATE is gone, the
            # DATA after it is the value, and a terminal ends the edge.
            cut = max((i for i, m in enumerate(array) if m[0] == INVALIDATE), default=-1)
            if cut >= 0:
                values = None
            data = tuple(m[1] for m in array[cut + 1 :] if m[0] == DATA)
            if data:
                values = data
            terminal = next((m for m in array if m[0] in (COMPLETE, ERROR)), None)
        after[edge] = _EdgeView(values, terminal)
    return after


def _gate_fn(group: str, known: frozenset[str]) -> Callable[[Ctx], None]:
    def run(ctx: Ctx) -> None:
        state = ctx.state if isinstance(ctx.state, _GateState) else _GateState()
        released: list[WireEdgeCause] = []
        issues: list[WireEdgeGroupIssue] = []
        for wave in ctx.deps[0].waves:
            for frame in wave:
                if getattr(frame, "group", None) != group:
                    continue  # another group's frame on the same channel
                if not (
                    isinstance(frame, WireEdgeCause)
                    and isinstance(frame.session, str)
                    and frame.session
                    and isinstance(frame.cause, int)
                    and not isinstance(frame.cause, bool)
                    and frame.cause >= 1
                    and isinstance(frame.edges, tuple)
                ):
                    issues.append(WireEdgeGroupIssue("malformed", reason="not a WireEdgeCause"))
                    continue
                if frame.session != state.session:
                    state.session, state.last = frame.session, 0
                session, cause = frame.session, frame.cause
                if cause <= state.last:
                    issues.append(WireEdgeGroupIssue("stale", session, cause, last=state.last))
                    continue
                gap = None
                if cause > state.last + 1:
                    gap = WireEdgeGroupIssue(
                        "gap", session, cause, missing_from=state.last + 1, missing_to=cause - 1
                    )
                state.last = cause
                try:
                    view = _view_after(frame, known, state.view)
                except _BadFrame as bad:
                    issues.append(WireEdgeGroupIssue("refused", session, cause, reason=str(bad)))
                    continue
                if gap is not None:
                    issues.append(gap)
                state.view = view
                released.append(frame)
        ctx.state = state
        if released or issues:
            ctx.down([(DATA, _GateOutput(tuple(released), tuple(issues), dict(state.view)))])

    return run


def _gate_values(ctx: Ctx) -> list[_GateOutput]:
    return [v for wave in ctx.deps[0].waves for v in wave if isinstance(v, _GateOutput)]


def _inbound_fn(edge: str) -> Callable[[Ctx], None]:
    def run(ctx: Ctx) -> None:
        outputs = _gate_values(ctx)
        if not ctx.state:
            ctx.state = True
            last = outputs[-1].view.get(edge) if outputs else None
            if last is not None and (last.values or last.terminal):
                snapshot: list[Message] = [(DATA, v) for v in (last.values or ())]
                if last.terminal is not None:
                    snapshot.append(last.terminal)
                ctx.down(snapshot)
            return
        for output in outputs:
            for frame in output.released:
                for entry in frame.edges:
                    if entry.edge_id == edge:
                        for array in entry.arrays:
                            ctx.down(list(array))

    return run


def _issues_fn(ctx: Ctx) -> None:
    outputs = _gate_values(ctx)
    if not ctx.state:
        ctx.state = True  # the first run does not repeat the gate's last issues
        return
    issues: list[Message] = [(DATA, issue) for output in outputs for issue in output.issues]
    if issues:
        ctx.down(issues)


def edges_of(frame: WireEdgeCause) -> Iterable[str]:
    """The edge ids a frame carries, in order."""

    return (entry.edge_id for entry in frame.edges)
