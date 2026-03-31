"""Memory patterns (roadmap §4.3).

Domain-layer helpers composed from GraphRefly primitives. ``vector_index`` uses
exact cosine search by default; HNSW can be wired as an optional dependency
through ``hnsw_factory``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from graphrefly.core.clock import monotonic_ns
from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import derived, state
from graphrefly.graph.graph import Graph

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from graphrefly.core.node import Node, NodeImpl

T = TypeVar("T")
TEntity = TypeVar("TEntity")
TRelation = TypeVar("TRelation", bound=str)
TMeta = TypeVar("TMeta")

CollectionPolicy = Literal["fifo", "lru"]
VectorBackend = Literal["flat", "hnsw"]


@dataclass(frozen=True, slots=True)
class LightCollectionEntry[T]:
    """Collection item metadata used by ``light_collection``."""

    id: str
    value: T
    created_at_ns: int
    last_access_ns: int


def decay(
    base_score: float, age_seconds: float, rate_per_second: float, min_score: float = 0.0
) -> float:
    """Exponential score decay with floor."""
    if not math.isfinite(base_score):
        return float(min_score)
    if not math.isfinite(age_seconds) or age_seconds <= 0:
        return max(min_score, float(base_score))
    if not math.isfinite(rate_per_second) or rate_per_second <= 0:
        return max(min_score, float(base_score))
    decayed = float(base_score) * math.exp(-rate_per_second * age_seconds)
    return max(min_score, decayed)


class LightCollection[T]:
    """FIFO/LRU collection without reactive scoring."""

    __slots__ = ("_entries", "_entries_node", "_max_size", "_policy")

    def __init__(
        self,
        *,
        max_size: int | None = None,
        policy: CollectionPolicy = "fifo",
        name: str | None = None,
    ) -> None:
        if max_size is not None and max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._max_size = max_size
        self._policy = policy
        self._entries: dict[str, LightCollectionEntry[T]] = {}
        self._entries_node: NodeImpl[Mapping[str, LightCollectionEntry[T]]] = state(
            MappingProxyType({}),
            name=name,
            describe_kind="state",
        )

    @property
    def entries(self) -> Node[Mapping[str, LightCollectionEntry[T]]]:
        return self._entries_node

    def _commit(self) -> None:
        snap = MappingProxyType(dict(self._entries))
        self._entries_node.down([(MessageType.DATA, snap)])

    def _evict_if_needed(self) -> None:
        if self._max_size is None:
            return
        while len(self._entries) > self._max_size:
            victim: LightCollectionEntry[T] | None = None
            for entry in self._entries.values():
                if victim is None:
                    victim = entry
                    continue
                lhs = entry.last_access_ns if self._policy == "lru" else entry.created_at_ns
                rhs = victim.last_access_ns if self._policy == "lru" else victim.created_at_ns
                if lhs < rhs:
                    victim = entry
            if victim is None:
                return
            self._entries.pop(victim.id, None)

    def upsert(self, item_id: str, value: T) -> None:
        now = monotonic_ns()
        prev = self._entries.get(item_id)
        self._entries[item_id] = LightCollectionEntry(
            id=item_id,
            value=value,
            created_at_ns=prev.created_at_ns if prev is not None else now,
            last_access_ns=now,
        )
        self._evict_if_needed()
        self._commit()

    def remove(self, item_id: str) -> None:
        if item_id not in self._entries:
            return
        del self._entries[item_id]
        self._commit()

    def clear(self) -> None:
        if not self._entries:
            return
        self._entries.clear()
        self._commit()

    def get(self, item_id: str) -> T | None:
        found = self._entries.get(item_id)
        if found is None:
            return None
        if self._policy == "lru":
            refreshed = LightCollectionEntry(
                id=found.id,
                value=found.value,
                created_at_ns=found.created_at_ns,
                last_access_ns=monotonic_ns(),
            )
            self._entries[item_id] = refreshed
            self._commit()
            return refreshed.value
        return found.value

    def has(self, item_id: str) -> bool:
        return item_id in self._entries


@dataclass(frozen=True, slots=True)
class CollectionEntry[T]:
    id: str
    value: T
    base_score: float
    created_at_ns: int
    last_access_ns: int


@dataclass(frozen=True, slots=True)
class RankedCollectionEntry[T]:
    id: str
    value: T
    base_score: float
    score: float
    created_at_ns: int
    last_access_ns: int


class CollectionGraph[T](Graph):
    """Scored memory collection represented as a graph."""

    __slots__ = (
        "_items",
        "_items_node",
        "_max_size",
        "_policy",
        "_score_fn",
        "_decay_rate",
        "_min_score",
    )

    def __init__(
        self,
        name: str,
        *,
        max_size: int | None = None,
        policy: CollectionPolicy = "lru",
        score: Callable[[T], float] | None = None,
        decay_rate: float = 0.0,
        min_score: float = 0.0,
        opts: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(name, opts)
        if max_size is not None and max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._max_size = max_size
        self._policy = policy
        self._score_fn = score if score is not None else (lambda _v: 1.0)
        self._decay_rate = decay_rate
        self._min_score = min_score
        self._items: dict[str, CollectionEntry[T]] = {}
        self._items_node: NodeImpl[Mapping[str, CollectionEntry[T]]] = state(
            MappingProxyType({}),
            name="items",
            describe_kind="state",
        )
        ranked = derived(
            [self._items_node],
            lambda deps, _a: self._ranked(deps[0]),
            initial=[],
            name="ranked",
        )
        size = derived(
            [self._items_node],
            lambda deps, _a: len(deps[0]),
            initial=0,
            name="size",
        )
        ranked.subscribe(lambda _msgs: None)
        size.subscribe(lambda _msgs: None)
        self.add("items", self._items_node)
        self.add("ranked", ranked)
        self.add("size", size)
        self.connect("items", "ranked")
        self.connect("items", "size")

    def _commit(self) -> None:
        self._items_node.down([(MessageType.DATA, MappingProxyType(dict(self._items)))])

    def _effective(self, entry: CollectionEntry[T], now_ns: int) -> float:
        age_seconds = (now_ns - entry.last_access_ns) / 1_000_000_000
        return decay(entry.base_score, age_seconds, self._decay_rate, self._min_score)

    def _ranked(
        self,
        snapshot: Mapping[str, CollectionEntry[T]],
    ) -> list[RankedCollectionEntry[T]]:
        now = monotonic_ns()
        out = [
            RankedCollectionEntry(
                id=row.id,
                value=row.value,
                base_score=row.base_score,
                score=self._effective(row, now),
                created_at_ns=row.created_at_ns,
                last_access_ns=row.last_access_ns,
            )
            for row in snapshot.values()
        ]
        out.sort(key=lambda row: (row.score, row.last_access_ns), reverse=True)
        return out

    def _evict_if_needed(self) -> None:
        if self._max_size is None:
            return
        while len(self._items) > self._max_size:
            now = monotonic_ns()
            victim: CollectionEntry[T] | None = None
            victim_score = float("inf")
            for row in self._items.values():
                score = self._effective(row, now)
                if score < victim_score:
                    victim = row
                    victim_score = score
                    continue
                if score == victim_score and victim is not None:
                    lhs = row.last_access_ns if self._policy == "lru" else row.created_at_ns
                    rhs = victim.last_access_ns if self._policy == "lru" else victim.created_at_ns
                    if lhs < rhs:
                        victim = row
            if victim is None:
                return
            self._items.pop(victim.id, None)

    def upsert(self, item_id: str, value: T, *, score: float | None = None) -> None:
        now = monotonic_ns()
        prev = self._items.get(item_id)
        base_score = float(score if score is not None else self._score_fn(value))
        self._items[item_id] = CollectionEntry(
            id=item_id,
            value=value,
            base_score=base_score,
            created_at_ns=prev.created_at_ns if prev is not None else now,
            last_access_ns=now,
        )
        self._evict_if_needed()
        self._commit()

    def remove(self, item_id: str) -> None:
        if item_id not in self._items:
            return
        del self._items[item_id]
        self._commit()

    def clear(self) -> None:
        if not self._items:
            return
        self._items.clear()
        self._commit()

    def get_item(self, item_id: str) -> CollectionEntry[T] | None:
        row = self._items.get(item_id)
        if row is None:
            return None
        if self._policy == "lru":
            refreshed = CollectionEntry(
                id=row.id,
                value=row.value,
                base_score=row.base_score,
                created_at_ns=row.created_at_ns,
                last_access_ns=monotonic_ns(),
            )
            self._items[item_id] = refreshed
            self._commit()
            return refreshed
        return row


@dataclass(frozen=True, slots=True)
class VectorRecord[TMeta]:
    id: str
    vector: tuple[float, ...]
    meta: TMeta | None


@dataclass(frozen=True, slots=True)
class VectorSearchResult[TMeta]:
    id: str
    score: float
    meta: TMeta | None


class HnswAdapter[TMeta](Protocol):
    def upsert(self, item_id: str, vector: Sequence[float], meta: TMeta | None = None) -> None: ...

    def remove(self, item_id: str) -> None: ...

    def clear(self) -> None: ...

    def search(self, query: Sequence[float], k: int) -> Sequence[VectorSearchResult[TMeta]]: ...


class VectorIndex[TMeta]:
    """Vector index with optional HNSW backend."""

    __slots__ = ("_backend", "_dimension", "_entries", "_entries_node", "_hnsw")

    def __init__(
        self,
        *,
        backend: VectorBackend = "flat",
        dimension: int | None = None,
        hnsw_factory: Callable[[], HnswAdapter[TMeta]] | None = None,
    ) -> None:
        self._backend = backend
        self._dimension = dimension
        self._entries: dict[str, VectorRecord[TMeta]] = {}
        self._entries_node: NodeImpl[Mapping[str, VectorRecord[TMeta]]] = state(
            MappingProxyType({}),
            name="vector_index",
            describe_kind="state",
        )
        self._hnsw = hnsw_factory() if backend == "hnsw" and hnsw_factory is not None else None
        if backend == "hnsw" and self._hnsw is None:
            msg = (
                'vector_index backend "hnsw" requires an optional dependency adapter; '
                "install your HNSW package and provide hnsw_factory."
            )
            raise ValueError(msg)

    @property
    def backend(self) -> VectorBackend:
        return self._backend

    @property
    def entries(self) -> Node[Mapping[str, VectorRecord[TMeta]]]:
        return self._entries_node

    def _commit(self) -> None:
        self._entries_node.down([(MessageType.DATA, MappingProxyType(dict(self._entries)))])

    def _assert_dimension(self, vector: Sequence[float]) -> None:
        if self._dimension is not None and len(vector) != self._dimension:
            raise ValueError(
                f"vector dimension mismatch: expected {self._dimension}, got {len(vector)}"
            )

    def upsert(self, item_id: str, vector: Sequence[float], meta: TMeta | None = None) -> None:
        self._assert_dimension(vector)
        row = VectorRecord(id=item_id, vector=tuple(float(v) for v in vector), meta=meta)
        self._entries[item_id] = row
        if self._backend == "hnsw":
            self._hnsw.upsert(item_id, vector, meta)  # type: ignore[union-attr]
        self._commit()

    def remove(self, item_id: str) -> None:
        if item_id not in self._entries:
            return
        del self._entries[item_id]
        if self._backend == "hnsw":
            self._hnsw.remove(item_id)  # type: ignore[union-attr]
        self._commit()

    def clear(self) -> None:
        if not self._entries:
            return
        self._entries.clear()
        if self._backend == "hnsw":
            self._hnsw.clear()  # type: ignore[union-attr]
        self._commit()

    def search(self, query: Sequence[float], k: int = 5) -> list[VectorSearchResult[TMeta]]:
        self._assert_dimension(query)
        if k <= 0:
            return []
        if self._backend == "hnsw":
            return list(self._hnsw.search(query, k))  # type: ignore[union-attr]
        query_tuple = tuple(float(v) for v in query)
        scored = [
            VectorSearchResult(
                id=row.id, score=_cosine_similarity(query_tuple, row.vector), meta=row.meta
            )
            for row in self._entries.values()
        ]
        scored.sort(key=lambda row: row.score, reverse=True)
        return scored[:k]


@dataclass(frozen=True, slots=True)
class KnowledgeEdge[TRelation: str]:
    from_id: str
    to_id: str
    relation: TRelation
    weight: float


class KnowledgeGraph[TEntity, TRelation: str](Graph):
    """Entity + relation graph represented as a GraphRefly graph."""

    __slots__ = ("_entities", "_relations", "_entities_node", "_edges_node")

    def __init__(self, name: str, opts: dict[str, Any] | None = None) -> None:
        super().__init__(name, opts)
        self._entities: dict[str, TEntity] = {}
        self._relations: list[KnowledgeEdge[TRelation]] = []
        self._entities_node: NodeImpl[Mapping[str, TEntity]] = state(
            MappingProxyType({}),
            name="entities",
            describe_kind="state",
        )
        self._edges_node: NodeImpl[tuple[KnowledgeEdge[TRelation], ...]] = state(
            (),
            name="edges",
            describe_kind="state",
        )
        adjacency = derived(
            [self._edges_node],
            lambda deps, _a: _build_adjacency(deps[0]),
            initial=MappingProxyType({}),
            name="adjacency",
        )
        adjacency.subscribe(lambda _msgs: None)
        self.add("entities", self._entities_node)
        self.add("edges", self._edges_node)
        self.add("adjacency", adjacency)
        self.connect("edges", "adjacency")

    def _commit_entities(self) -> None:
        self._entities_node.down([(MessageType.DATA, MappingProxyType(dict(self._entities)))])

    def _commit_edges(self) -> None:
        self._edges_node.down([(MessageType.DATA, tuple(self._relations))])

    def upsert_entity(self, entity_id: str, value: TEntity) -> None:
        self._entities[entity_id] = value
        self._commit_entities()

    def remove_entity(self, entity_id: str) -> None:
        had_entity = entity_id in self._entities
        if had_entity:
            del self._entities[entity_id]
        next_edges = [
            edge
            for edge in self._relations
            if edge.from_id != entity_id and edge.to_id != entity_id
        ]
        if not had_entity and len(next_edges) == len(self._relations):
            return
        self._relations = next_edges
        self._commit_entities()
        self._commit_edges()

    def link(self, from_id: str, to_id: str, relation: TRelation, weight: float = 1.0) -> None:
        key = (from_id, to_id, relation)
        replaced = False
        next_edges: list[KnowledgeEdge[TRelation]] = []
        for edge in self._relations:
            if (edge.from_id, edge.to_id, edge.relation) == key:
                next_edges.append(
                    KnowledgeEdge(
                        from_id=from_id, to_id=to_id, relation=relation, weight=float(weight)
                    )
                )
                replaced = True
            else:
                next_edges.append(edge)
        if not replaced:
            next_edges.append(
                KnowledgeEdge(from_id=from_id, to_id=to_id, relation=relation, weight=float(weight))
            )
        self._relations = next_edges
        self._commit_edges()

    def unlink(self, from_id: str, to_id: str, relation: TRelation | None = None) -> None:
        next_edges = [
            edge
            for edge in self._relations
            if not (
                edge.from_id == from_id
                and edge.to_id == to_id
                and (relation is None or edge.relation == relation)
            )
        ]
        if len(next_edges) == len(self._relations):
            return
        self._relations = next_edges
        self._commit_edges()

    def related(
        self, entity_id: str, relation: TRelation | None = None
    ) -> list[KnowledgeEdge[TRelation]]:
        return [
            edge
            for edge in self._relations
            if (edge.from_id == entity_id or edge.to_id == entity_id)
            and (relation is None or edge.relation == relation)
        ]


def light_collection(
    *,
    max_size: int | None = None,
    policy: CollectionPolicy = "fifo",
    name: str | None = None,
) -> LightCollection[Any]:
    """Create an unscored FIFO/LRU collection."""
    return LightCollection(max_size=max_size, policy=policy, name=name)


def collection(
    name: str,
    *,
    max_size: int | None = None,
    policy: CollectionPolicy = "lru",
    score: Callable[[Any], float] | None = None,
    decay_rate: float = 0.0,
    min_score: float = 0.0,
    opts: dict[str, Any] | None = None,
) -> CollectionGraph[Any]:
    """Create a scored memory collection graph."""
    return CollectionGraph(
        name,
        max_size=max_size,
        policy=policy,
        score=score,
        decay_rate=decay_rate,
        min_score=min_score,
        opts=opts,
    )


def vector_index(
    *,
    backend: VectorBackend = "flat",
    dimension: int | None = None,
    hnsw_factory: Callable[[], HnswAdapter[Any]] | None = None,
) -> VectorIndex[Any]:
    """Create a vector index with optional HNSW backend adapter."""
    return VectorIndex(
        backend=backend,
        dimension=dimension,
        hnsw_factory=hnsw_factory,
    )


def knowledge_graph(
    name: str,
    *,
    opts: dict[str, Any] | None = None,
) -> KnowledgeGraph[Any, str]:
    """Create a knowledge graph domain graph."""
    return KnowledgeGraph(name, opts=opts)


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    n = max(len(a), len(b))
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(n):
        avf = float(a[i]) if i < len(a) else 0.0
        bvf = float(b[i]) if i < len(b) else 0.0
        dot += avf * bvf
        na += avf * avf
        nb += bvf * bvf
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def _build_adjacency(
    edges: Sequence[KnowledgeEdge[Any]],
) -> Mapping[str, tuple[KnowledgeEdge[Any], ...]]:
    raw: dict[str, list[KnowledgeEdge[Any]]] = {}
    for edge in edges:
        raw.setdefault(edge.from_id, []).append(edge)
    return MappingProxyType({key: tuple(vals) for key, vals in raw.items()})


__all__ = [
    "CollectionEntry",
    "CollectionGraph",
    "CollectionPolicy",
    "HnswAdapter",
    "KnowledgeEdge",
    "KnowledgeGraph",
    "LightCollection",
    "LightCollectionEntry",
    "RankedCollectionEntry",
    "VectorBackend",
    "VectorIndex",
    "VectorRecord",
    "VectorSearchResult",
    "collection",
    "decay",
    "knowledge_graph",
    "light_collection",
    "vector_index",
]
