"""Shared helpers for the kernel scenario tests (graphrefly-py:D2)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from graphrefly._kernel import Ctx, Graph, Message, Report, TraceEntry


def _payload(value: object) -> str:
    if isinstance(value, BaseException):
        return json.dumps(str(value))
    return json.dumps(value, separators=(",", ":"))


def fmt(entry: TraceEntry) -> str:
    """The reference kernel's trace format: ``sink <- node: [PENDING, DATA(1)]``."""

    body = ", ".join(f"{m[0]}({_payload(m[1])})" if len(m) > 1 else str(m[0]) for m in entry.msgs)
    return f"{entry.to} <- {entry.sender}: [{body}]"


def during(graph: Graph, body: Callable[[], object]) -> list[str]:
    """The arrays delivered to every sink while ``body`` runs, formatted."""

    assert graph.trace is not None
    start = len(graph.trace)
    body()
    return [fmt(entry) for entry in graph.trace[start:]]


@dataclass
class Producer:
    """A dependency-free node that keeps its ctx, so later ``down`` calls are its waves."""

    ctx: Ctx | None = None

    def __call__(self, ctx: Ctx) -> None:
        self.ctx = ctx

    def down(self, array: list[Message]) -> None:
        assert self.ctx is not None, "subscribe the producer first"
        self.ctx.down(array)


@dataclass
class Recorder:
    """A sink callback that keeps every array it receives, in order."""

    arrays: list[tuple[Message, ...]] = field(default_factory=list)

    def __call__(self, array: tuple[Message, ...]) -> None:
        self.arrays.append(array)

    def kinds(self) -> list[list[str]]:
        return [[message[0] for message in array] for array in self.arrays]


@dataclass
class Harness:
    graph: Graph
    reports: list[Report]

    def sink(self, node_id: str, sink_id: str | None = None) -> Recorder:
        recorder = Recorder()
        self.graph.subscribe(sink_id or f"sink:{node_id}", node_id, recorder)
        return recorder


@pytest.fixture
def harness() -> Harness:
    reports: list[Report] = []
    return Harness(Graph(report=reports.append, trace=True), reports)
