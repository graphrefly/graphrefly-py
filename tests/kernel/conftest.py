"""Shared helpers for the kernel scenario tests (graphrefly-py:D2)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from graphrefly._kernel import Graph, Message, Report


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
