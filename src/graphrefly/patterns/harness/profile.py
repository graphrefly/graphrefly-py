"""Harness-specific graph profiling (roadmap §9.0).

Extends :func:`graph_profile` with harness domain counters:
queue depths, strategy entries, retry/reingestion tracker sizes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from graphrefly.core.runner import is_runner_registered
from graphrefly.graph.profile import graph_profile

if TYPE_CHECKING:
    from graphrefly.patterns.harness.loop import HarnessGraph


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HarnessProfileResult:
    """Harness-specific profile extending the base graph profile."""

    # Base graph profile fields
    node_count: int
    edge_count: int
    subgraph_count: int
    nodes: tuple[Any, ...]
    total_value_size_bytes: int
    hotspots: tuple[Any, ...]
    # Harness-specific
    queue_depths: dict[str, int]
    strategy_entries: int
    total_retries: int
    total_reingestions: int
    # Runner diagnostic (session doc: surfaces registered=False immediately)
    runner_registered: bool


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def harness_profile(
    harness: HarnessGraph,
    *,
    top_n: int = 10,
) -> HarnessProfileResult:
    """Profile a harness graph with domain-specific counters.

    Args:
        harness: The HarnessGraph to profile.
        top_n: Limit hotspot list (default 10).

    Returns:
        Harness profile with queue depths, strategy stats, and tracker sizes.
    """
    base = graph_profile(harness, top_n=top_n)

    queue_depths: dict[str, int] = {}
    for route, topic in harness.queues.items():
        queue_depths[route] = len(topic.retained())

    strat_node_val = harness.strategy.node.get()
    strategy_entries = len(strat_node_val) if strat_node_val else 0

    return HarnessProfileResult(
        node_count=base.node_count,
        edge_count=base.edge_count,
        subgraph_count=base.subgraph_count,
        nodes=base.nodes,
        total_value_size_bytes=base.total_value_size_bytes,
        hotspots=base.hotspots,
        queue_depths=queue_depths,
        strategy_entries=strategy_entries,
        total_retries=harness.total_retries.get() or 0,
        total_reingestions=harness.total_reingestions.get() or 0,
        runner_registered=is_runner_registered(),
    )
