"""Graph profiling and inspection utilities.

Provides per-node memory estimation, connectivity stats, and hotspot
detection. Non-invasive — reads from ``describe()`` and node internals
without modifying state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from graphrefly.core.node import NodeImpl
from graphrefly.graph.sizeof import sizeof

if TYPE_CHECKING:
    from graphrefly.graph.graph import Graph


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NodeProfile:
    """Per-node profile entry."""

    path: str
    type: str
    status: str
    value_size_bytes: int
    subscriber_count: int
    dep_count: int


@dataclass(frozen=True, slots=True)
class GraphProfileResult:
    """Aggregate graph profile."""

    node_count: int
    edge_count: int
    subgraph_count: int
    nodes: tuple[NodeProfile, ...]
    total_value_size_bytes: int
    hotspots: tuple[NodeProfile, ...]


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def graph_profile(
    graph: Graph,
    *,
    top_n: int = 10,
) -> GraphProfileResult:
    """Profile a graph's memory and connectivity characteristics.

    Uses ``describe(detail="standard")`` for node metadata and direct
    ``NodeImpl`` access for subscriber counts and cached values.

    Args:
        graph: The graph to profile.
        top_n: Limit hotspot list (default 10).

    Returns:
        Aggregate profile with per-node details and hotspots.
    """
    desc = graph.describe(detail="standard")

    # Build path→Node lookup via _collect_observe_targets (same as describe uses).
    targets: list[tuple[str, Any]] = []
    if hasattr(graph, "_collect_observe_targets"):
        graph._collect_observe_targets("", targets)
    path_to_node: dict[str, Any] = {p: n for p, n in targets}

    profiles: list[NodeProfile] = []

    for path, node_desc in desc.get("nodes", {}).items():
        nd = path_to_node.get(path)
        impl = nd if isinstance(nd, NodeImpl) else None

        value_size_bytes = sizeof(impl.get()) if impl else 0
        subscriber_count = impl._sink_count if impl else 0
        dep_count = len(node_desc.get("deps", []))

        profiles.append(
            NodeProfile(
                path=path,
                type=node_desc.get("type", "unknown"),
                status=node_desc.get("status", "unknown"),
                value_size_bytes=value_size_bytes,
                subscriber_count=subscriber_count,
                dep_count=dep_count,
            )
        )

    total_value_size_bytes = sum(p.value_size_bytes for p in profiles)

    hotspots = tuple(sorted(profiles, key=lambda p: p.value_size_bytes, reverse=True)[:top_n])

    return GraphProfileResult(
        node_count=len(profiles),
        edge_count=len(desc.get("edges", [])),
        subgraph_count=len(desc.get("subgraphs", [])),
        nodes=tuple(profiles),
        total_value_size_bytes=total_value_size_bytes,
        hotspots=hotspots,
    )
