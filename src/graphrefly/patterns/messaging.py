"""Messaging patterns (roadmap §4.2).

Pulsar-inspired messaging features modeled as graph factories:
- ``topic()`` for append-only topic streams
- ``subscription()`` for cursor-based consumers
- ``job_queue()`` for queue claim/ack flow
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import derived, effect, state
from graphrefly.extra.data_structures import (
    reactive_list,
    reactive_log,
    reactive_map,
)
from graphrefly.graph.graph import Graph
from graphrefly.patterns._internal import domain_meta
from graphrefly.patterns._internal import keepalive as _keepalive

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_MAX_PER_PUMP = 2_147_483_647


def _messaging_meta(kind: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return domain_meta("messaging", kind, extra)


def _tuple_snapshot(raw: Any) -> tuple[Any, ...]:
    if isinstance(raw, tuple):
        return raw
    if isinstance(raw, list):
        return tuple(raw)
    return ()


def _require_non_negative_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative integer")
    if not isinstance(value, int):
        raise ValueError(f"{label} must be a non-negative integer")
    if value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


class TopicGraph(Graph):
    """Append-only topic graph with retained event log and latest value node."""

    __slots__ = ("_log", "events", "latest")

    def __init__(
        self,
        name: str,
        *,
        opts: dict[str, Any] | None = None,
        retained_limit: int | None = None,
    ) -> None:
        super().__init__(name, opts)
        self._log = reactive_log(max_size=retained_limit, name="events")
        self.events = self._log.entries
        self.add("events", self.events)

        def compute_latest(deps: list[Any], _actions: Any) -> Any:
            entries = _tuple_snapshot(deps[0])
            return entries[-1] if len(entries) > 0 else None

        self.latest = derived(
            [self.events],
            compute_latest,
            initial=None,
            name="latest",
            meta=_messaging_meta("topic_latest"),
        )
        self.add("latest", self.latest)
        self.connect("events", "latest")
        self.add_disposer(_keepalive(self.latest))

    def publish(self, value: Any) -> None:
        self._log.append(value)

    def retained(self) -> tuple[Any, ...]:
        return _tuple_snapshot(self.events.get())


class SubscriptionGraph(Graph):
    """Cursor-based consumer graph over a topic."""

    __slots__ = ("available", "cursor", "source")

    def __init__(
        self,
        name: str,
        topic_graph: TopicGraph,
        *,
        opts: dict[str, Any] | None = None,
        cursor: int = 0,
    ) -> None:
        super().__init__(name, opts)
        initial_cursor = _require_non_negative_int(cursor, label="subscription cursor")
        self.mount("topic", topic_graph)
        topic_events = topic_graph.events
        self.source = derived(
            [topic_events],
            lambda deps, _a: deps[0],
            initial=topic_events.get(),
            name="source",
            meta=_messaging_meta("subscription_source"),
        )
        self.add("source", self.source)
        self.cursor = state(
            initial_cursor,
            name="cursor",
            describe_kind="state",
            meta=_messaging_meta("subscription_cursor"),
        )
        self.add("cursor", self.cursor)

        def compute_available(deps: list[Any], _actions: Any) -> tuple[Any, ...]:
            idx = max(0, int(deps[1] if deps[1] is not None else 0))
            entries = _tuple_snapshot(deps[0])
            return entries[idx:]

        self.available = derived(
            [self.source, self.cursor],
            compute_available,
            initial=(),
            name="available",
            meta=_messaging_meta("subscription_available"),
        )
        self.add("available", self.available)
        self.connect("topic::events", "source")
        self.connect("source", "available")
        self.connect("cursor", "available")
        self.add_disposer(_keepalive(self.source))
        self.add_disposer(_keepalive(self.available))

    def ack(self, count: int | None = None) -> int:
        available = _tuple_snapshot(self.available.get())
        total = len(available)
        requested = (
            total
            if count is None
            else _require_non_negative_int(
                count,
                label="subscription ack count",
            )
        )
        step = min(total, requested)
        current_raw = self.cursor.get()
        current = int(current_raw) if current_raw is not None else 0
        if step <= 0:
            return current
        next_cursor = current + step
        self.cursor.down([(MessageType.DATA, next_cursor)])
        return next_cursor

    def pull(self, limit: int | None = None, *, ack: bool = False) -> tuple[Any, ...]:
        available = _tuple_snapshot(self.available.get())
        n = (
            len(available)
            if limit is None
            else _require_non_negative_int(
                limit,
                label="subscription pull limit",
            )
        )
        out = available[:n]
        if ack and len(out) > 0:
            self.ack(len(out))
        return out


type JobState = str


@dataclass(frozen=True, slots=True)
class JobEnvelope:
    """Queued/inflight job record used by :class:`JobQueueGraph`."""

    id: str
    payload: Any
    attempts: int
    metadata: Mapping[str, Any]
    state: JobState


class JobQueueGraph(Graph):
    """Queue graph with claim/ack/nack primitives."""

    __slots__ = ("_jobs", "_pending", "_seq", "depth", "jobs", "pending")

    def __init__(self, name: str, *, opts: dict[str, Any] | None = None) -> None:
        super().__init__(name, opts)
        self._pending = reactive_list(name="pending")
        self._jobs = reactive_map(name="jobs")
        self._seq = 0
        self.pending = self._pending.items
        self.jobs = self._jobs.entries
        self.add("pending", self.pending)
        self.add("jobs", self.jobs)
        self.depth = derived(
            [self.pending],
            lambda deps, _a: len(_tuple_snapshot(deps[0])),
            initial=0,
            name="depth",
            meta=_messaging_meta("queue_depth"),
        )
        self.add("depth", self.depth)
        self.connect("pending", "depth")
        self.add_disposer(_keepalive(self.depth))

    def _job_get(self, job_id: str) -> JobEnvelope | None:
        snapshot = self._jobs.entries.get()
        if snapshot is None:
            return None
        return snapshot.get(job_id) if isinstance(snapshot, MappingProxyType) else None

    def enqueue(
        self,
        payload: Any,
        *,
        job_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if job_id is None:
            self._seq += 1
            job_id = f"{self.name}-{self._seq}"
        if self._job_get(job_id) is not None:
            raise ValueError(f'job_queue("{self.name}"): duplicate job id {job_id!r}')
        job = JobEnvelope(
            id=job_id,
            payload=payload,
            attempts=0,
            metadata=MappingProxyType(dict(metadata or {})),
            state="queued",
        )
        self._jobs.set(job_id, job)
        self._pending.append(job_id)
        return job_id

    def claim(self, limit: int = 1) -> tuple[JobEnvelope, ...]:
        max_items = _require_non_negative_int(limit, label="job queue claim limit")
        if max_items == 0:
            return ()
        out: list[JobEnvelope] = []
        while len(out) < max_items:
            ids = _tuple_snapshot(self.pending.get())
            if len(ids) == 0:
                break
            job_id = self._pending.pop(0)
            current = self._job_get(job_id)
            if current is None or current.state != "queued":
                continue
            inflight = JobEnvelope(
                id=current.id,
                payload=current.payload,
                attempts=current.attempts + 1,
                metadata=current.metadata,
                state="inflight",
            )
            self._jobs.set(job_id, inflight)
            out.append(inflight)
        return tuple(out)

    def ack(self, job_id: str) -> bool:
        current = self._job_get(job_id)
        if current is None or current.state != "inflight":
            return False
        self._jobs.delete(job_id)
        return True

    def nack(self, job_id: str, *, requeue: bool = True) -> bool:
        current = self._job_get(job_id)
        if current is None or current.state != "inflight":
            return False
        if requeue:
            self._jobs.set(
                job_id,
                JobEnvelope(
                    id=current.id,
                    payload=current.payload,
                    attempts=current.attempts,
                    metadata=current.metadata,
                    state="queued",
                ),
            )
            self._pending.append(job_id)
            return True
        self._jobs.delete(job_id)
        return True


class JobFlowGraph(Graph):
    """Autonomous multi-stage queue chain graph."""

    __slots__ = ("_completed", "_queues", "_stage_names", "completed", "completed_count")

    def __init__(
        self,
        name: str,
        *,
        opts: dict[str, Any] | None = None,
        stages: tuple[str, ...] | list[str] | None = None,
        max_per_pump: int | None = None,
    ) -> None:
        super().__init__(name, opts)
        raw = tuple(stages or ("incoming", "processing", "done"))
        stage_names = tuple(v.strip() for v in raw)
        if len(stage_names) < 2:
            raise ValueError(f'job_flow("{name}"): requires at least 2 stages')
        if len(set(stage_names)) != len(stage_names):
            raise ValueError(f'job_flow("{name}"): stage names must be unique')
        self._stage_names = stage_names
        self._queues: dict[str, JobQueueGraph] = {}
        for stage in self._stage_names:
            queue = job_queue(f"{name}-{stage}")
            self._queues[stage] = queue
            self.mount(stage, queue)
        self._completed = reactive_log(name="completed")
        self.completed = self._completed.entries
        self.add("completed", self.completed)
        self.completed_count = derived(
            [self.completed],
            lambda deps, _a: len(_tuple_snapshot(deps[0])),
            initial=0,
            name="completed_count",
            meta=_messaging_meta("job_flow_completed_count"),
        )
        self.add("completedCount", self.completed_count)
        self.connect("completed", "completedCount")
        self.add_disposer(_keepalive(self.completed_count))

        raw_max = DEFAULT_MAX_PER_PUMP if max_per_pump is None else max_per_pump
        max_items = max(1, _require_non_negative_int(raw_max, label="job flow max_per_pump"))
        for index, stage in enumerate(self._stage_names):
            current = self.queue(stage)
            nxt = (
                self.queue(self._stage_names[index + 1])
                if index + 1 < len(self._stage_names)
                else None
            )

            def run_stage(
                _deps: list[Any],
                _actions: Any,
                *,
                s: str = stage,
                cur: JobQueueGraph = current,
                nxt_q: JobQueueGraph | None = nxt,
                m: int = max_items,
            ) -> None:
                moved = 0
                while moved < m:
                    claimed = cur.claim(1)
                    if len(claimed) == 0:
                        break
                    job = claimed[0]
                    if nxt_q is not None:
                        nxt_q.enqueue(
                            job.payload,
                            metadata={**dict(job.metadata), "job_flow_from": s},
                        )
                    else:
                        self._completed.append(job)
                    cur.ack(job.id)
                    moved += 1

            pump = effect(
                [current.pending],
                run_stage,
                name=f"pump_{stage}",
                meta=_messaging_meta("job_flow_pump"),
            )
            self.add(f"pump_{stage}", pump)
            self.connect(f"{stage}::pending", f"pump_{stage}")
            self.add_disposer(_keepalive(pump))

    def stages(self) -> tuple[str, ...]:
        return self._stage_names

    def queue(self, stage: str) -> JobQueueGraph:
        try:
            return self._queues[stage]
        except KeyError as exc:
            raise ValueError(f'job_flow("{self.name}"): unknown stage {stage!r}') from exc

    def enqueue(
        self,
        payload: Any,
        *,
        job_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        first = self._stage_names[0]
        return self.queue(first).enqueue(payload, job_id=job_id, metadata=metadata)

    def retained_completed(self) -> tuple[JobEnvelope, ...]:
        return tuple(_tuple_snapshot(self.completed.get()))


class TopicBridgeGraph(Graph):
    """Autonomous cursor-based topic relay graph."""

    __slots__ = ("_source_sub", "_target", "bridged_count")

    def __init__(
        self,
        name: str,
        source_topic: TopicGraph,
        target_topic: TopicGraph,
        *,
        opts: dict[str, Any] | None = None,
        cursor: int = 0,
        max_per_pump: int | None = None,
        map_fn: Any | None = None,
    ) -> None:
        super().__init__(name, opts)
        self._source_sub = subscription(f"{name}-subscription", source_topic, cursor=cursor)
        self._target = target_topic
        self.mount("subscription", self._source_sub)
        self.bridged_count = state(
            0,
            name="bridged_count",
            describe_kind="state",
            meta=_messaging_meta("topic_bridge_count"),
        )
        self.add("bridgedCount", self.bridged_count)

        raw_max = DEFAULT_MAX_PER_PUMP if max_per_pump is None else max_per_pump
        max_items = max(1, _require_non_negative_int(raw_max, label="topic bridge max_per_pump"))
        mapper = map_fn or (lambda value: value)

        def run_bridge(_deps: list[Any], _actions: Any) -> None:
            available = self._source_sub.pull(max_items, ack=True)
            if len(available) == 0:
                return
            bridged = 0
            for value in available:
                mapped = mapper(value)
                if mapped is None:
                    continue
                self._target.publish(mapped)
                bridged += 1
            if bridged > 0:
                current_raw = self.bridged_count.get()
                current = int(current_raw) if current_raw is not None else 0
                self.bridged_count.down([(MessageType.DATA, current + bridged)])

        pump = effect(
            [self._source_sub.available],
            run_bridge,
            name="pump",
            meta=_messaging_meta("topic_bridge_pump"),
        )
        self.add("pump", pump)
        self.connect("subscription::available", "pump")
        self.add_disposer(_keepalive(pump))


def topic(
    name: str,
    *,
    opts: dict[str, Any] | None = None,
    retained_limit: int | None = None,
) -> TopicGraph:
    """Create a Pulsar-inspired topic graph (retained append-only stream)."""
    return TopicGraph(name, opts=opts, retained_limit=retained_limit)


def subscription(
    name: str,
    topic_graph: TopicGraph,
    *,
    opts: dict[str, Any] | None = None,
    cursor: int = 0,
) -> SubscriptionGraph:
    """Create a cursor-based subscription graph over a topic."""
    return SubscriptionGraph(name, topic_graph, opts=opts, cursor=cursor)


def job_queue(name: str, *, opts: dict[str, Any] | None = None) -> JobQueueGraph:
    """Create a Pulsar-inspired job queue graph with claim/ack/nack workflow."""
    return JobQueueGraph(name, opts=opts)


def job_flow(
    name: str,
    *,
    opts: dict[str, Any] | None = None,
    stages: tuple[str, ...] | list[str] | None = None,
    max_per_pump: int | None = None,
) -> JobFlowGraph:
    """Create an autonomous multi-stage queue chain graph."""
    return JobFlowGraph(name, opts=opts, stages=stages, max_per_pump=max_per_pump)


def topic_bridge(
    name: str,
    source_topic: TopicGraph,
    target_topic: TopicGraph,
    *,
    opts: dict[str, Any] | None = None,
    cursor: int = 0,
    max_per_pump: int | None = None,
    map_fn: Any | None = None,
) -> TopicBridgeGraph:
    """Create an autonomous cursor-based topic relay graph."""
    return TopicBridgeGraph(
        name,
        source_topic,
        target_topic,
        opts=opts,
        cursor=cursor,
        max_per_pump=max_per_pump,
        map_fn=map_fn,
    )


__all__ = [
    "JobEnvelope",
    "JobFlowGraph",
    "JobQueueGraph",
    "SubscriptionGraph",
    "TopicBridgeGraph",
    "TopicGraph",
    "job_flow",
    "job_queue",
    "subscription",
    "topic",
    "topic_bridge",
]
