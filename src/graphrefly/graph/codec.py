"""GraphCodec — pluggable serialization for graph snapshots (Phase 8.6).

The codec interface decouples snapshot format from graph internals.
Default is JSON (current behavior). DAG-CBOR and compressed variants
ship as optional codecs.

Tiered representation:
  HOT  — Python objects (live propagation, no codec involved)
  WARM — DAG-CBOR in-memory buffer (lazy hydration, delta checkpoints)
  COLD — Arrow/Parquet (bulk storage, ML pipelines, archival)
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Core codec interface
# ---------------------------------------------------------------------------


@runtime_checkable
class GraphCodec(Protocol):
    """Encode/decode graph snapshots to/from binary.

    Implementations must be deterministic: ``encode(x)`` always produces
    the same bytes for the same input.
    """

    @property
    def content_type(self) -> str:
        """MIME-like content type identifier (e.g. ``application/dag-cbor+zstd``)."""
        ...

    @property
    def name(self) -> str:
        """Human-readable name for diagnostics."""
        ...

    def encode(self, snapshot: dict[str, Any]) -> bytes:
        """Encode a snapshot to binary."""
        ...

    def decode(self, buffer: bytes) -> dict[str, Any]:
        """Decode binary back to a snapshot."""
        ...


@runtime_checkable
class LazyGraphCodec(GraphCodec, Protocol):
    """Extended codec that supports lazy (on-demand) node decoding."""

    def decode_lazy(self, buffer: bytes) -> dict[str, Any]:
        """Decode envelope and topology; defer node value decoding to access time."""
        ...


# ---------------------------------------------------------------------------
# Delta checkpoint types (requires V0 — Phase 6.0)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeltaCheckpoint:
    """A delta checkpoint: only the nodes that changed since last checkpoint."""

    seq: int
    name: str
    base_seq: int
    nodes: dict[str, dict[str, Any]]
    removed: tuple[str, ...]
    edges_added: tuple[dict[str, str], ...]
    edges_removed: tuple[dict[str, str], ...]
    timestamp_ns: int


@dataclass(frozen=True, slots=True)
class WALEntry:
    """WAL entry: either a full snapshot or a delta."""

    type: str  # "full" | "delta"
    snapshot: dict[str, Any] | None = None
    delta: DeltaCheckpoint | None = None
    seq: int = 0


# ---------------------------------------------------------------------------
# Eviction policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvictionPolicy:
    """Policy for evicting dormant subgraphs to reduce memory."""

    idle_timeout_ms: int
    codec: GraphCodec | None = None


@dataclass(frozen=True, slots=True)
class EvictedSubgraphInfo:
    """Metadata about an evicted subgraph, exposed via describe()."""

    evicted: bool
    last_active_ns: int
    serialized_bytes: int
    codec_name: str


# ---------------------------------------------------------------------------
# JSON codec (default)
# ---------------------------------------------------------------------------


class JsonCodec:
    """Default JSON codec. Wraps ``json.dumps``/``json.loads`` with
    deterministic key ordering.
    """

    @property
    def content_type(self) -> str:
        return "application/json"

    @property
    def name(self) -> str:
        return "json"

    def encode(self, snapshot: dict[str, Any]) -> bytes:
        return json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def decode(self, buffer: bytes) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(buffer.decode("utf-8"))
        return result


JSON_CODEC = JsonCodec()


# ---------------------------------------------------------------------------
# DAG-CBOR codec (stub — requires cbor2)
# ---------------------------------------------------------------------------


def create_dag_cbor_codec(cbor_module: Any) -> GraphCodec:
    """Create a DAG-CBOR codec.

    Requires ``cbor2`` as a dependency. ~40-50% smaller than JSON,
    deterministic encoding.

    Args:
        cbor_module: A module with ``encode(value) -> bytes`` and
            ``decode(bytes) -> Any`` callables.
    """

    class _DagCborCodec:
        @property
        def content_type(self) -> str:
            return "application/dag-cbor"

        @property
        def name(self) -> str:
            return "dag-cbor"

        def encode(self, snapshot: dict[str, Any]) -> bytes:
            result: bytes = cbor_module.encode(snapshot)
            return result

        def decode(self, buffer: bytes) -> dict[str, Any]:
            result: dict[str, Any] = cbor_module.decode(buffer)
            return result

    return _DagCborCodec()


def create_dag_cbor_zstd_codec(
    cbor_module: Any,
    zstd_module: Any,
) -> GraphCodec:
    """Create a DAG-CBOR + zstd codec. ~80-90% smaller than JSON.

    Args:
        cbor_module: Module with ``encode``/``decode``.
        zstd_module: Module with ``compress``/``decompress``.
    """

    class _DagCborZstdCodec:
        @property
        def content_type(self) -> str:
            return "application/dag-cbor+zstd"

        @property
        def name(self) -> str:
            return "dag-cbor-zstd"

        def encode(self, snapshot: dict[str, Any]) -> bytes:
            result: bytes = zstd_module.compress(cbor_module.encode(snapshot))
            return result

        def decode(self, buffer: bytes) -> dict[str, Any]:
            result: dict[str, Any] = cbor_module.decode(zstd_module.decompress(buffer))
            return result

    return _DagCborZstdCodec()


# ---------------------------------------------------------------------------
# Codec negotiation
# ---------------------------------------------------------------------------


def negotiate_codec(
    local_preference: list[GraphCodec],
    remote_content_types: list[str],
) -> GraphCodec | None:
    """Negotiate a common codec between two peers.

    Each peer advertises its supported codecs (ordered by preference).
    Returns the first codec supported by both, or ``None``.
    """
    remote_set = set(remote_content_types)
    for codec in local_preference:
        if codec.content_type in remote_set:
            return codec
    return None


# ---------------------------------------------------------------------------
# WAL helpers
# ---------------------------------------------------------------------------


def replay_wal(entries: list[WALEntry]) -> dict[str, Any]:
    """Reconstruct a snapshot from a WAL (full snapshot + sequence of deltas).

    Applies deltas in order on top of the base snapshot.

    Args:
        entries: Ordered WAL entries (must start with a full snapshot).

    Returns:
        Reconstructed snapshot dict.

    Raises:
        ValueError: If the WAL is empty or doesn't start with a full snapshot.
    """
    if not entries:
        raise ValueError("WAL is empty — need at least one full snapshot")

    first = entries[0]
    if first.type != "full" or first.snapshot is None:
        raise ValueError("WAL must start with a full snapshot")

    # Deep clone so we can mutate.
    import copy

    result = copy.deepcopy(first.snapshot)

    for entry in entries[1:]:
        if entry.type == "full" and entry.snapshot is not None:
            result = copy.deepcopy(entry.snapshot)
            continue

        if entry.type != "delta" or entry.delta is None:
            continue

        delta = entry.delta

        # Apply node changes.
        nodes = result.setdefault("nodes", {})
        for name, patch in delta.nodes.items():
            if name in nodes:
                nodes[name]["value"] = patch.get("value")
                if "meta" in patch:
                    nodes[name]["meta"] = patch["meta"]
            else:
                nodes[name] = patch

        # Remove nodes.
        for name in delta.removed:
            nodes.pop(name, None)

        # Apply edge changes.
        edges = result.setdefault("edges", [])
        for edge in delta.edges_added:
            edges.append(edge)
        for edge in delta.edges_removed:
            with contextlib.suppress(ValueError):
                edges.remove(edge)

    return result
