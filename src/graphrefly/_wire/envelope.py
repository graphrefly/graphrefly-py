"""The wire bridge's envelope layer over the Python kernel (graphrefly:D803 (8), C-1c).

R-bridge leaves the transport to the host; this layer is the package's transport
adapter over canonical protobuf bytes, in the shape D803 gives the envelope lanes:

- received envelopes come from a node (the host's receiving end, as bytes);
- facts such as ack timeouts are writes to a state node the bridge depends on;
- outbound envelopes come out of a node, and a transport sink sends their bytes.

The bridge numbers each outbound DATA envelope, keeps it pending until the peer acks
it, and on an ack-timeout fact for the pending attempt sends it again with the next
attempt, until ``max_attempts``, when it is exhausted. A received DATA envelope is
acked and its payload goes out as a frame or a value. Stale, mismatched and malformed
facts never become terminals: the stale and mismatched ones change nothing and the
malformed ones are issues. No timer is set; :func:`wire_bridge_ack_driver` derives the
timeout facts from a clock node the host writes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, NotRequired, TypedDict

from graphrefly._kernel import DATA, Ctx, Graph
from graphrefly._wire.protobuf import (
    Envelope,
    Metadata,
    ProtobufError,
    WireEdgeCause,
    decode_envelope,
    encode_envelope,
)

type Guard = Callable[[Callable[[Ctx], None]], Callable[[Ctx], None]]


def _same(fn: Callable[[Ctx], None]) -> Callable[[Ctx], None]:
    return fn


@dataclass(frozen=True, slots=True)
class WireBridgeAckTimeout:
    """An ack-timeout fact: the pending ``attempt`` of envelope ``seq`` timed out."""

    seq: int
    attempt: int
    observed_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class WireBridgeStatus:
    """What the bridge holds: its pending attempts as ``(seq, attempt)`` pairs and counts."""

    state: Literal["open", "waiting", "exhausted"]
    session_id: str
    next_seq: int
    pending: tuple[tuple[int, int], ...] = ()
    acked: int = 0
    nacked: int = 0
    retries: int = 0
    exhausted: int = 0


@dataclass(frozen=True, slots=True)
class WireBridgeIssue:
    """A bridge issue: never a terminal of a bridge node.

    ``code`` is ``invalid_bytes`` (received bytes the canonical codec refused;
    ``category`` is its D497 category), ``invalid_timeout`` (a malformed ack-timeout
    fact), ``invalid_payload`` (an outbound value that is neither bytes nor a frame),
    ``exhausted`` (an envelope ran out of attempts), ``nack`` or ``peer_error``.
    """

    code: Literal[
        "invalid_bytes", "invalid_timeout", "invalid_payload", "exhausted", "nack", "peer_error"
    ]
    message: str
    category: str | None = None
    seq: int | None = None


class WireBridgeEvent(TypedDict):
    """A wire fact; common kinds and field names match the TypeScript and Rust bridge."""

    kind: Literal["outbound", "inbound", "ack", "nack", "timeout", "retry", "exhausted", "invalid"]
    envelope: NotRequired[Envelope]
    outbound: NotRequired[Envelope]
    ackForSeq: NotRequired[int]
    seq: NotRequired[int]
    attempt: NotRequired[int]
    delayMs: NotRequired[int]
    error: NotRequired[str]


class WireBridgeIndexedEvent(TypedDict):
    """One bridge event with a monotonic index, retained across external subscription changes."""

    index: int
    event: WireBridgeEvent


@dataclass(slots=True)
class _Pending:
    envelope: Envelope
    attempt: int


@dataclass(slots=True)
class _BridgeState:
    next_index: int = 1
    next_seq: int = 1
    pending: dict[int, _Pending] = field(default_factory=dict)
    acked: int = 0
    nacked: int = 0
    retries: int = 0
    exhausted: int = 0
    last: Literal["open", "waiting", "exhausted"] = "open"


@dataclass(frozen=True, slots=True)
class _BridgeOutput:
    events: tuple[WireBridgeIndexedEvent, ...]
    envelopes: tuple[Envelope, ...]
    frames: tuple[WireEdgeCause, ...]
    values: tuple[bytes, ...]
    status: WireBridgeStatus
    issues: tuple[WireBridgeIssue, ...]


@dataclass(slots=True)
class Released:
    """A bundle's host-lifetime switch: once set, its node functions do nothing."""

    released: bool = False


@dataclass(frozen=True, slots=True)
class BridgeNodes:
    core: str
    events: str
    status_ledger: str
    ack_timeouts: str
    outbound_bytes: str
    frames: str
    values: str
    status: str
    issues: str


def wire_bridge(
    g: Graph,
    node_id: str,
    *,
    session_id: str,
    outbound: Sequence[str] = (),
    received: str | None = None,
    max_attempts: int = 2,
    released: Released | None = None,
    guard: Guard = _same,
) -> BridgeNodes:
    """Build a bridge ``node_id`` over ``outbound`` nodes (whose DATA values are frames or
    bytes to send) and ``received`` (a node whose DATA values are received bytes)."""

    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        msg = "max_attempts must be a positive int"
        raise ValueError(msg)
    if not isinstance(session_id, str) or not session_id:
        msg = "session_id must be a non-empty str"
        raise ValueError(msg)
    switch = released if released is not None else Released()
    ack_timeouts = f"{node_id}.ack_timeouts"
    g.state(ack_timeouts)
    n_out = len(outbound)
    deps = [*outbound, ack_timeouts, *([received] if received is not None else [])]

    def envelope(state: _BridgeState, attempt: int, seq: int, **body: object) -> Envelope:
        meta = Metadata(seq, 0, f"{session_id}:{seq}", attempt, max_attempts)
        return Envelope(session_id, meta, "data", **body)  # type: ignore[arg-type]

    def setup(ctx: Ctx) -> None:
        if ctx.state is None:
            ctx.state = _BridgeState()

    def run(ctx: Ctx) -> None:
        if switch.released:
            return
        state = ctx.state
        assert isinstance(state, _BridgeState)
        events: list[WireBridgeEvent] = []
        out: list[Envelope] = []
        frames: list[WireEdgeCause] = []
        values: list[bytes] = []
        issues: list[WireBridgeIssue] = []
        changed = False
        for index, dep in enumerate(ctx.deps):
            items = [v for wave in dep.waves for v in wave]
            if not items:
                continue
            changed = True
            if index < n_out:
                for item in items:
                    seq = state.next_seq
                    if isinstance(item, WireEdgeCause):
                        env = envelope(state, 1, seq, edge_cause=item)
                    elif isinstance(item, bytes | bytearray):
                        env = envelope(state, 1, seq, value=bytes(item))
                    else:
                        issues.append(
                            WireBridgeIssue(
                                "invalid_payload", "wireBridge: outbound values are bytes or frames"
                            )
                        )
                        continue
                    state.next_seq += 1
                    state.pending[seq] = _Pending(env, 1)
                    out.append(env)
                    events.append({"kind": "outbound", "envelope": env})
            elif index == n_out:
                for fact in items:
                    _timeout(state, fact, out, issues, events, max_attempts, session_id)
            else:
                for raw in items:
                    _receive(
                        state, raw, out, frames, values, issues, events, session_id, max_attempts
                    )
        if not changed:
            return
        if state.pending and state.last != "exhausted":
            state.last = "waiting"
        elif not state.pending and state.last == "waiting":
            state.last = "open"
        status = WireBridgeStatus(
            state.last,
            session_id,
            state.next_seq,
            tuple((seq, p.attempt) for seq, p in sorted(state.pending.items())),
            state.acked,
            state.nacked,
            state.retries,
            state.exhausted,
        )
        for issue in issues:
            if issue.code not in ("nack", "exhausted"):
                events.append({"kind": "invalid", "error": issue.message})
        indexed: list[WireBridgeIndexedEvent] = []
        for event in events:
            indexed.append({"index": state.next_index, "event": event})
            state.next_index += 1
        output = _BridgeOutput(
            tuple(indexed), tuple(out), tuple(frames), tuple(values), status, tuple(issues)
        )
        ctx.down([(DATA, output)])

    g.node(
        node_id,
        deps,
        guard(run),
        complete="manual",
        error="manual",
        invalidate="manual",
        setup=setup,
    )
    nodes = BridgeNodes(
        node_id,
        f"{node_id}.events",
        f"{node_id}.status_ledger",
        ack_timeouts,
        f"{node_id}.outbound_bytes",
        f"{node_id}.frames",
        f"{node_id}.values",
        f"{node_id}.status",
        f"{node_id}.issues",
    )
    _project(g, nodes.events, node_id, guard, lambda o: list(o.events))
    _project(
        g, nodes.outbound_bytes, node_id, guard, lambda o: [encode_envelope(e) for e in o.envelopes]
    )
    # Frames and values are quiet: a wave of the core that received only an ack marks
    # nothing downstream, and what did arrive goes out as a wave of its own.
    _project(g, nodes.frames, node_id, guard, lambda o: list(o.frames), first=False, quiet=True)
    _project(g, nodes.values, node_id, guard, lambda o: list(o.values), first=False, quiet=True)
    _project_status(g, nodes, session_id, guard)
    _project(g, nodes.issues, node_id, guard, lambda o: list(o.issues), first=False)
    return nodes


def _project(
    g: Graph,
    node_id: str,
    core: str,
    guard: Guard,
    pick: Callable[[_BridgeOutput], list[object]],
    *,
    first: bool = True,
    quiet: bool = False,
) -> None:
    def run(ctx: Ctx) -> None:
        if not first and not ctx.state:
            ctx.state = True  # the first run does not repeat what the core last sent
            return
        ctx.state = True
        items = [
            item
            for wave in ctx.deps[0].waves
            for o in wave
            if isinstance(o, _BridgeOutput)
            for item in pick(o)
        ]
        if items:
            ctx.down([(DATA, item) for item in items])

    g.node(node_id, [core], guard(run), complete="manual", error="manual", quiet=quiet)


def _project_status(g: Graph, nodes: BridgeNodes, session_id: str, guard: Guard) -> None:
    initial: dict[str, Any] = {
        "highWater": 0,
        "status": asdict(WireBridgeStatus("open", session_id, 1)),
    }
    # Use JSON arrays in the ledger, not the public status dataclass's tuples.
    initial["status"]["pending"] = []
    g.state(nodes.status_ledger, initial)

    def setup(ctx: Ctx) -> Callable[[], None]:
        if ctx.state is None:
            saved = ctx.deps[2].value
            assert isinstance(saved, dict)
            ctx.state = {"highWater": saved["highWater"], "status": dict(saved["status"])}

        def cleanup() -> None:
            g.set(nodes.status_ledger, ctx.state)

        return cleanup

    def run(ctx: Ctx) -> None:
        working = ctx.state
        assert isinstance(working, dict)
        events = [event for wave in ctx.deps[0].waves for event in wave]
        latest = ctx.deps[1].value
        for indexed in events:
            if not isinstance(indexed, dict) or indexed["index"] <= working["highWater"]:
                continue
            working["highWater"] = indexed["index"]
            # Core's cumulative state includes facts that arrived while this projection
            # was inactive. High water suppresses the last-array replay on reactivation.
            if isinstance(latest, _BridgeOutput):
                status = asdict(latest.status)
                status["pending"] = [list(p) for p in latest.status.pending]
                working["status"] = status
        saved = working["status"]
        ctx.down(
            [(DATA, WireBridgeStatus(**{**saved, "pending": tuple(map(tuple, saved["pending"]))}))]
        )

    g.node(
        nodes.status,
        [nodes.events, nodes.core, nodes.status_ledger],
        guard(run),
        complete="manual",
        error="manual",
        setup=setup,
    )


def _timeout(
    state: _BridgeState,
    fact: object,
    out: list[Envelope],
    issues: list[WireBridgeIssue],
    events: list[WireBridgeEvent],
    max_attempts: int,
    session_id: str,
) -> None:
    if not isinstance(fact, WireBridgeAckTimeout):
        issues.append(
            WireBridgeIssue(
                "invalid_timeout", "wireBridge: ack-timeout facts are WireBridgeAckTimeout"
            )
        )
        return
    for label, value in (("seq", fact.seq), ("attempt", fact.attempt)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            issues.append(
                WireBridgeIssue(
                    "invalid_timeout", f"wireBridge: ack-timeout command {label} must be positive"
                )
            )
            return
    pending = state.pending.get(fact.seq)
    if pending is None or pending.attempt != fact.attempt:
        return  # stale or mismatched: a fail-closed no-op
    events.append({"kind": "timeout", "seq": fact.seq, "attempt": fact.attempt})
    if pending.attempt < max_attempts:
        attempt = pending.attempt + 1
        meta = pending.envelope.metadata
        retry = Envelope(
            session_id,
            Metadata(meta.seq, meta.cursor, meta.idempotency_key, attempt, max_attempts),
            "data",
            value=pending.envelope.value,
            edge_cause=pending.envelope.edge_cause,
        )
        state.pending[fact.seq] = _Pending(retry, attempt)
        state.retries += 1
        events.append(
            {
                "kind": "retry",
                "seq": fact.seq,
                "attempt": attempt,
                "delayMs": 0,
                "error": "ack timeout",
            }
        )
        out.append(retry)
        events.append({"kind": "outbound", "envelope": retry})
        return
    del state.pending[fact.seq]
    state.exhausted += 1
    state.last = "exhausted"
    events.append(
        {"kind": "exhausted", "seq": fact.seq, "attempt": pending.attempt, "error": "ack timeout"}
    )
    issues.append(
        WireBridgeIssue(
            "exhausted",
            f"wireBridge: ack timeout for seq {fact.seq} after {pending.attempt} attempts",
            seq=fact.seq,
        )
    )


def _receive(
    state: _BridgeState,
    raw: object,
    out: list[Envelope],
    frames: list[WireEdgeCause],
    values: list[bytes],
    issues: list[WireBridgeIssue],
    events: list[WireBridgeEvent],
    session_id: str,
    max_attempts: int,
) -> None:
    if not isinstance(raw, bytes | bytearray):
        issues.append(
            WireBridgeIssue("invalid_bytes", "wireBridge: received values are bytes", "malformed")
        )
        return
    try:
        env = decode_envelope(bytes(raw))
    except ProtobufError as error:
        issues.append(WireBridgeIssue("invalid_bytes", f"wireBridge: {error}", error.category))
        return
    meta = env.metadata
    events.append({"kind": "inbound", "envelope": env})
    if env.payload == "data":
        seq = state.next_seq
        state.next_seq += 1
        out.append(
            Envelope(
                session_id,
                Metadata(seq, 0, f"{session_id}:{seq}", 1, max_attempts, ack_for_seq=meta.seq),
                "ack",
            )
        )
        events.append({"kind": "outbound", "envelope": out[-1]})
        if env.edge_cause is not None:
            frames.append(env.edge_cause)
        elif env.value is not None:
            values.append(env.value)
    elif env.payload == "ack":
        pending = state.pending.pop(meta.ack_for_seq or 0, None)
        if pending is not None:
            state.acked += 1
            events.append(
                {
                    "kind": "ack",
                    "ackForSeq": meta.ack_for_seq or 0,
                    "envelope": env,
                    "outbound": pending.envelope,
                }
            )
    elif env.payload == "nack":
        pending = state.pending.pop(meta.ack_for_seq or 0, None)
        if pending is not None:
            state.nacked += 1
            events.append(
                {
                    "kind": "nack",
                    "ackForSeq": meta.ack_for_seq or 0,
                    "envelope": env,
                    "outbound": pending.envelope,
                    "error": "nack",
                }
            )
            issues.append(
                WireBridgeIssue(
                    "nack", f"wireBridge: nack for seq {meta.ack_for_seq}", seq=meta.ack_for_seq
                )
            )
    elif env.payload == "error":
        issues.append(WireBridgeIssue("peer_error", "wireBridge: the peer sent an error"))


@dataclass(frozen=True, slots=True)
class WireBridgeAckDriverStatus:
    """The ack driver's status: its timeout and the last timeout fact it derived."""

    state: Literal["idle", "ready", "issues"]
    timeout_ms: int
    last_timeout: WireBridgeAckTimeout | None = None


@dataclass(frozen=True, slots=True)
class WireBridgeAckDriverIssue:
    """An ack driver issue, such as a clock fact that is not a non-decreasing int."""

    code: Literal["invalid_clock"]
    message: str


@dataclass(slots=True)
class _DriverState:
    clock: int | None = None
    first_seen: dict[tuple[int, int], int] = field(default_factory=dict)
    fired: set[tuple[int, int]] = field(default_factory=set)
    last_timeout: WireBridgeAckTimeout | None = None
    recent: tuple[WireBridgeAckTimeout, ...] = ()
    has_issue: bool = False


@dataclass(frozen=True, slots=True)
class _DriverOutput:
    timeouts: tuple[WireBridgeAckTimeout, ...]
    recent: tuple[WireBridgeAckTimeout, ...]
    issues: tuple[WireBridgeAckDriverIssue, ...]
    status: WireBridgeAckDriverStatus


@dataclass(frozen=True, slots=True)
class DriverNodes:
    core: str
    timeouts: str
    status: str
    issues: str


def wire_bridge_ack_driver(
    g: Graph,
    node_id: str,
    bridge: BridgeNodes,
    *,
    clock: str,
    timeout_ms: int,
    released: Released | None = None,
    guard: Guard = _same,
) -> DriverNodes:
    """Derive ack-timeout facts from ``clock`` (a node of non-decreasing ints, in ms) and
    the bridge's pending attempts, and write each into the bridge's ack-timeout state."""

    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
        msg = "timeout_ms must be a positive int"
        raise ValueError(msg)
    switch = released if released is not None else Released()

    def run(ctx: Ctx) -> None:
        if switch.released:
            return
        state = ctx.state if isinstance(ctx.state, _DriverState) else _DriverState()
        ctx.state = state
        issues: list[WireBridgeAckDriverIssue] = []
        for value in (v for wave in ctx.deps[0].waves for v in wave):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                issues.append(
                    WireBridgeAckDriverIssue(
                        "invalid_clock",
                        "wire_bridge_ack_driver clock facts must be non-negative integers",
                    )
                )
            elif state.clock is not None and value < state.clock:
                issues.append(
                    WireBridgeAckDriverIssue(
                        "invalid_clock",
                        "wire_bridge_ack_driver clock facts must be monotonic non-decreasing",
                    )
                )
            else:
                state.clock = value
        bridge_status = ctx.deps[1].value
        pending = (
            set(bridge_status.pending) if isinstance(bridge_status, WireBridgeStatus) else set()
        )
        for key in list(state.first_seen):
            if key not in pending:
                del state.first_seen[key]
        timeouts: list[WireBridgeAckTimeout] = []
        if state.clock is not None:
            for key in sorted(pending):
                start = state.first_seen.setdefault(key, state.clock)
                if key not in state.fired and state.clock - start >= timeout_ms:
                    state.fired.add(key)
                    timeouts.append(WireBridgeAckTimeout(key[0], key[1], state.clock))
        for fact in timeouts:
            g.set(bridge.ack_timeouts, fact)  # a request: the bridge sees it in a later wave
        if timeouts:
            state.last_timeout = timeouts[-1]
            state.recent = tuple(timeouts)
        state.has_issue = state.has_issue or bool(issues)
        status = WireBridgeAckDriverStatus(
            "issues" if issues else "ready" if state.clock is not None else "idle",
            timeout_ms,
            state.last_timeout,
        )
        ctx.down([(DATA, _DriverOutput(tuple(timeouts), state.recent, tuple(issues), status))])

    g.node(node_id, [clock, bridge.status], guard(run), complete="manual", error="manual")
    nodes = DriverNodes(node_id, f"{node_id}.timeouts", f"{node_id}.status", f"{node_id}.issues")

    def projection(
        pick: Callable[[_DriverOutput], Sequence[object]],
        first: bool,
        initial: Callable[[_DriverOutput], Sequence[object]] | None = None,
    ) -> Callable[[Ctx], None]:
        def run_projection(ctx: Ctx) -> None:
            outputs = [o for w in ctx.deps[0].waves for o in w if isinstance(o, _DriverOutput)]
            if not ctx.state:
                ctx.state = True
                if not first:
                    return  # the first run does not repeat the last issues
                if initial is not None:
                    # The first run gives what the driver derived last, for a late subscriber.
                    items = list(initial(outputs[-1])) if outputs else []
                    if items:
                        ctx.down([(DATA, item) for item in items])
                    return
            items = [i for o in outputs for i in pick(o)]
            if items:
                ctx.down([(DATA, item) for item in items])

        return run_projection

    g.node(
        nodes.timeouts,
        [node_id],
        guard(projection(lambda o: o.timeouts, True, lambda o: o.recent)),
    )
    g.node(nodes.issues, [node_id], guard(projection(lambda o: o.issues, False)))
    g.node(nodes.status, [node_id], guard(projection(lambda o: (o.status,), True)))
    return nodes
