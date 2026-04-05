"""CQRS patterns (roadmap §4.5).

Composition layer over reactive_log (3.2), pipeline/sagas (4.1), event bus (4.2),
projections (4.3). Guards (1.5) enforce command/query boundary.

- ``cqrs(name, opts?)`` → ``CqrsGraph`` — top-level factory
- ``CqrsGraph.command(name, handler)`` — write-only node; guard rejects ``observe``
- ``CqrsGraph.event(name)`` — backed by ``reactive_log``; append-only
- ``CqrsGraph.projection(name, events, reducer, initial)`` — read-only derived;
  guard rejects ``write``
- ``CqrsGraph.saga(name, events, handler)`` — event-driven side effects
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from graphrefly.core.clock import wall_clock_ns
from graphrefly.core.guard import policy
from graphrefly.core.node import node
from graphrefly.core.protocol import MessageType, batch
from graphrefly.core.sugar import derived, state
from graphrefly.extra.data_structures import Versioned, reactive_log
from graphrefly.graph.graph import Graph

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from graphrefly.core.guard import GuardFn
    from graphrefly.core.node import Node

# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

COMMAND_GUARD: GuardFn = policy(
    lambda allow, deny: [
        allow("write"),
        allow("signal"),
        deny("observe"),
    ]
)

PROJECTION_GUARD: GuardFn = policy(
    lambda allow, deny: [
        allow("observe"),
        allow("signal"),
        deny("write"),
    ]
)

EVENT_GUARD: GuardFn = policy(
    lambda allow, deny: [
        allow("observe"),
        allow("signal"),
        deny("write"),
    ]
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cqrs_meta(kind: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"cqrs": True, "cqrs_type": kind}
    if extra:
        out.update(extra)
    return out


@dataclass(slots=True)
class _EventLogEntry:
    """Internal: a registered event log and its guarded node."""

    log: Any
    node: Any


def _keepalive(n: Any) -> Callable[[], None]:
    """Keep dep wiring alive; returns unsubscribe handle for cleanup."""
    return n.subscribe(lambda _msgs: None)


def _tuple_snapshot(raw: Any) -> tuple[Any, ...]:
    if isinstance(raw, tuple):
        return raw
    if isinstance(raw, list):
        return tuple(raw)
    return ()


# ---------------------------------------------------------------------------
# Event envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CqrsEvent:
    """Immutable envelope for events emitted by command handlers.

    Attributes:
        type: Event name.
        payload: Arbitrary event data.
        timestamp_ns: Wall-clock nanoseconds (via ``wall_clock_ns()``).
        seq: Monotonic sequence within this ``CqrsGraph`` instance.
    """

    type: str
    payload: Any
    timestamp_ns: int
    seq: int
    v0: dict[str, Any] | None = None
    """V0 identity of the event log node at append time (§6.0b)."""


# ---------------------------------------------------------------------------
# Event store adapter
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventStoreCursor:
    """Opaque cursor for resumable event loading."""

    timestamp_ns: int
    seq: int


@dataclass(frozen=True, slots=True)
class LoadEventsResult:
    """Result of :meth:`EventStoreAdapter.load_events`."""

    events: list[CqrsEvent] = field(default_factory=list)
    cursor: EventStoreCursor | None = None


class EventStoreAdapter:
    """Interface for pluggable event persistence.

    :meth:`persist` is **synchronous** — called inline during ``dispatch``.
    :meth:`load_events` is async and backs :meth:`CqrsGraph.rebuild_projection`.
    """

    def persist(self, event: CqrsEvent) -> None:
        raise NotImplementedError

    async def load_events(
        self, event_type: str, cursor: EventStoreCursor | None = None
    ) -> LoadEventsResult:
        """Load persisted events; if *cursor* is set, resume from that position."""
        raise NotImplementedError

    async def flush(self) -> None:
        """Optional: flush buffered writes. Default is a no-op."""


class MemoryEventStore(EventStoreAdapter):
    """In-memory event store (default)."""

    def __init__(self) -> None:
        self._store: dict[str, list[CqrsEvent]] = {}

    def persist(self, event: CqrsEvent) -> None:
        self._store.setdefault(event.type, []).append(event)

    async def load_events(
        self, event_type: str, cursor: EventStoreCursor | None = None
    ) -> LoadEventsResult:
        events = list(self._store.get(event_type, []))
        since_ts: int | None = cursor.timestamp_ns if cursor is not None else None
        since_seq: int | None = cursor.seq if cursor is not None else None
        if since_ts is not None:
            events = [
                e
                for e in events
                if e.timestamp_ns > since_ts
                or (
                    e.timestamp_ns == since_ts
                    and e.seq > (since_seq if since_seq is not None else -1)
                )
            ]
        new_cursor: EventStoreCursor | None = (
            EventStoreCursor(timestamp_ns=events[-1].timestamp_ns, seq=events[-1].seq)
            if events
            else cursor
        )
        return LoadEventsResult(events=events, cursor=new_cursor)

    def clear(self) -> None:
        self._store.clear()


# ---------------------------------------------------------------------------
# Handler types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandActions:
    """Actions available inside a command handler."""

    emit: Callable[[str, Any], None]


type CommandHandler = Callable[[Any, CommandActions], None]

type ProjectionReducer = Callable[[Any, list[CqrsEvent]], Any]
"""Projection reducer folds events into a read model.

**Purity contract:** Reducers MUST be pure — return a new state value
without mutating ``state`` or any event. The ``state`` parameter is the
original ``initial`` on every invocation (full event-sourcing replay).
"""

type SagaHandler = Callable[[CqrsEvent], None]


# ---------------------------------------------------------------------------
# CqrsGraph
# ---------------------------------------------------------------------------


class CqrsGraph(Graph):
    """CQRS graph container — composes events, commands, projections, and sagas."""

    __slots__ = (
        "_command_handlers",
        "_event_logs",
        "_event_store",
        "_keepalive_disposers",
        "_projections",
        "_sagas",
        "_seq",
    )

    def __init__(self, name: str, opts: dict[str, Any] | None = None) -> None:
        super().__init__(name, opts)
        self._event_logs: dict[str, _EventLogEntry] = {}
        self._command_handlers: dict[str, CommandHandler] = {}
        self._projections: set[str] = set()
        self._sagas: set[str] = set()
        self._keepalive_disposers: list[Callable[[], None]] = []
        self._event_store: EventStoreAdapter | None = None
        self._seq = 0

    def destroy(self) -> None:
        for dispose in self._keepalive_disposers:
            dispose()
        self._keepalive_disposers.clear()
        super().destroy()

    # -- Events ---------------------------------------------------------------

    def event(self, name: str) -> Node[Any]:
        """Register a named event stream backed by ``reactive_log``.

        Guard denies external ``write`` — only commands append internally.
        """
        existing = self._event_logs.get(name)
        if existing is not None:
            return existing.node

        log = reactive_log(name=name)
        entries = log.entries

        def pass_through(deps: list[Any], _actions: Any) -> Any:
            return deps[0]

        guarded = node(
            [entries],
            pass_through,
            name=name,
            describe_kind="state",
            meta=_cqrs_meta("event", {"event_name": name}),
            guard=EVENT_GUARD,
            initial=entries.get(),
        )
        self.add(name, guarded)
        self._keepalive_disposers.append(_keepalive(guarded))
        self._event_logs[name] = _EventLogEntry(log=log, node=guarded)
        return guarded

    def _append_event(self, event_name: str, payload: Any) -> None:
        """Internal: append to an event log, auto-registering if needed."""
        entry = self._event_logs.get(event_name)
        if entry is None:
            self.event(event_name)
            entry = self._event_logs[event_name]
        # Terminal guard: reject dispatch to completed/errored streams.
        status = entry.node.status
        if status in ("completed", "errored"):
            msg = f'Cannot dispatch to terminated event stream "{event_name}" (status: {status}).'
            raise RuntimeError(msg)
        self._seq += 1
        nv = entry.log.entries.v
        evt = CqrsEvent(
            type=event_name,
            payload=payload,
            timestamp_ns=wall_clock_ns(),
            seq=self._seq,
            v0={"id": nv.id, "version": nv.version} if nv is not None else None,
        )
        entry.log.append(evt)
        if self._event_store is not None:
            self._event_store.persist(evt)

    # -- Commands -------------------------------------------------------------

    def command(self, name: str, handler: CommandHandler) -> Node[Any]:
        """Register a command with its handler. Guard denies ``observe`` (write-only).

        The command node carries dynamic ``meta.error`` — a reactive companion
        that holds the last handler error (or ``None`` on success).

        Use ``dispatch(name, payload)`` to execute.
        """
        cmd_node = state(
            None,
            name=name,
            describe_kind="state",
            meta={
                **_cqrs_meta("command", {"command_name": name}),
                "error": None,
            },
            guard=COMMAND_GUARD,
        )
        self.add(name, cmd_node)
        self._command_handlers[name] = handler
        return cmd_node

    def dispatch(self, command_name: str, payload: Any) -> None:
        """Execute a registered command.

        Wraps the entire dispatch in ``batch()`` so the command node DATA and
        all emitted events settle atomically.

        If the handler throws, ``meta.error`` on the command node is set to
        the error and the exception is re-raised.
        """
        handler = self._command_handlers.get(command_name)
        if handler is None:
            msg = f'Unknown command: "{command_name}". Register with .command() first.'
            raise ValueError(msg)
        cmd_node = self.resolve(command_name)

        def emit(event_name: str, event_payload: Any) -> None:
            self._append_event(event_name, event_payload)

        with batch():
            cmd_node.down([(MessageType.DATA, payload)], internal=True)
            try:
                handler(payload, CommandActions(emit=emit))
                cmd_node.meta["error"].down([(MessageType.DATA, None)], internal=True)
            except Exception as exc:
                cmd_node.meta["error"].down([(MessageType.DATA, exc)], internal=True)
                raise

    # -- Projections ----------------------------------------------------------

    def projection(
        self,
        name: str,
        event_names: Sequence[str],
        reducer: ProjectionReducer,
        initial: Any,
    ) -> Node[Any]:
        """Register a read-only projection derived from event streams.

        Guard denies ``write`` — value is computed from events only.

        **Purity contract:** The ``reducer`` must be a pure function — it
        receives the original ``initial`` on every invocation. Never mutate
        ``initial``; always return a new value.
        """
        event_nodes = []
        for ename in event_names:
            if ename not in self._event_logs:
                self.event(ename)
            event_nodes.append(self._event_logs[ename].node)

        captured_initial = initial

        def compute(deps: list[Any], _actions: Any) -> Any:
            all_events: list[CqrsEvent] = []
            for dep in deps:
                snap = dep
                entries = _tuple_snapshot(snap.value if isinstance(snap, Versioned) else ())
                all_events.extend(entries)
            all_events.sort(key=lambda e: (e.timestamp_ns, e.seq))
            return reducer(captured_initial, all_events)

        proj_node = derived(
            event_nodes,
            compute,
            name=name,
            meta=_cqrs_meta(
                "projection",
                {
                    "projection_name": name,
                    "source_events": list(event_names),
                },
            ),
            guard=PROJECTION_GUARD,
            initial=initial,
        )
        self.add(name, proj_node)
        for ename in event_names:
            self.connect(ename, name)
        self._keepalive_disposers.append(_keepalive(proj_node))
        self._projections.add(name)
        return proj_node

    # -- Sagas ----------------------------------------------------------------

    def saga(
        self,
        name: str,
        event_names: Sequence[str],
        handler: SagaHandler,
    ) -> Node[Any]:
        """Register an event-driven side effect.

        Runs handler for each **new** event from the specified streams (tracks
        last-processed entry count per stream).

        The saga node carries dynamic ``meta.error`` — a reactive companion
        that holds the last handler error (or ``None`` on success). Handler
        errors do not propagate out of the saga run (the event cursor still
        advances so the same entry is not delivered twice).
        """
        event_nodes = []
        for ename in event_names:
            if ename not in self._event_logs:
                self.event(ename)
            event_nodes.append(self._event_logs[ename].node)

        # Track last-processed entry count per event to only process new entries
        last_counts: dict[str, int] = {}

        saga_ref: list[Any] = [None]

        def run_saga(deps: list[Any], _actions: Any) -> None:
            saga_n = saga_ref[0]
            err_node = saga_n.meta["error"]
            for i, dep in enumerate(deps):
                snap = dep
                ename = event_names[i]
                entries = _tuple_snapshot(snap.value if isinstance(snap, Versioned) else ())
                last_count = last_counts.get(ename, 0)
                if len(entries) > last_count:
                    new_entries = entries[last_count:]
                    for entry in new_entries:
                        try:
                            handler(entry)
                            err_node.down([(MessageType.DATA, None)], internal=True)
                        except Exception as exc:
                            err_node.down([(MessageType.DATA, exc)], internal=True)
                    last_counts[ename] = len(entries)

        saga_node = node(
            event_nodes,
            run_saga,
            name=name,
            describe_kind="effect",
            meta={
                **_cqrs_meta(
                    "saga",
                    {
                        "saga_name": name,
                        "source_events": list(event_names),
                    },
                ),
                "error": None,
            },
        )
        saga_ref[0] = saga_node
        self.add(name, saga_node)
        for ename in event_names:
            self.connect(ename, name)
        self._keepalive_disposers.append(_keepalive(saga_node))
        self._sagas.add(name)
        return saga_node

    # -- Event store ----------------------------------------------------------

    def use_event_store(self, adapter: EventStoreAdapter) -> None:
        """Wire a pluggable event store adapter."""
        self._event_store = adapter

    async def rebuild_projection(
        self,
        event_names: Sequence[str],
        reducer: ProjectionReducer,
        initial: Any,
    ) -> Any:
        """Replay persisted events through a reducer to rebuild a read model.

        Requires an event store adapter wired via ``use_event_store()``.
        """
        if self._event_store is None:
            msg = "No event store wired. Call use_event_store() first."
            raise RuntimeError(msg)
        all_events: list[CqrsEvent] = []
        for ename in event_names:
            result = await self._event_store.load_events(ename)
            all_events.extend(result.events)
        all_events.sort(key=lambda e: (e.timestamp_ns, e.seq))
        return reducer(initial, all_events)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def cqrs(name: str, opts: dict[str, Any] | None = None) -> CqrsGraph:
    """Create a CQRS graph container.

    Example:
        ```python
        app = cqrs("orders")
        app.event("order_placed")
        app.command("place_order", lambda payload, actions: actions.emit("order_placed", payload))
        app.projection("order_count", ["order_placed"], lambda _s, events: len(events), 0)
        app.dispatch("place_order", {"id": "1"})
        ```
    """
    return CqrsGraph(name, opts)
