"""Composite data patterns (roadmap §3.2b).

These helpers compose existing primitives without introducing new protocol semantics.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from graphrefly.core.dynamic_node import dynamic_node
from graphrefly.core.node import Node
from graphrefly.core.protocol import MessageType, batch
from graphrefly.core.sugar import derived, state
from graphrefly.extra.data_structures import ReactiveMapBundle, reactive_map
from graphrefly.extra.sources import for_each, from_any, of
from graphrefly.extra.tier1 import merge
from graphrefly.extra.tier2 import switch_map

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True, slots=True)
class VerifiableBundle:
    """Result of :func:`verifiable`."""

    node: Node[Any]
    verified: Node[Any]
    trigger: Node[Any] | None


def verifiable(
    source: Any,
    verify_fn: Callable[[Any], Any],
    *,
    trigger: Any | None = None,
    auto_verify: bool = False,
    initial_verified: Any = None,
) -> VerifiableBundle:
    """Compose a value node with a reactive verification companion.

    Args:
        source: Value source (`Node`, scalar, awaitable, iterable, async iterable).
        verify_fn: Verification function returning a `NodeInput`.
        trigger: Optional reactive trigger; each `DATA` triggers verification.
        auto_verify: When true, source value changes also trigger verification.
        initial_verified: Initial value for the verification companion.
    """
    source_node = from_any(source)
    has_source_versioning = source_node.v is not None
    meta_opts: dict[str, Any] = {"source_version": None} if has_source_versioning else {}
    verified_node = state(initial_verified, **({"meta": meta_opts} if meta_opts else {}))

    trigger_node: Node[Any] | None = None
    if trigger is not None and auto_verify:
        trigger_node = merge(from_any(trigger), source_node)
    elif trigger is not None:
        trigger_node = from_any(trigger)
    elif auto_verify:
        trigger_node = source_node

    if trigger_node is not None:
        verify_stream = switch_map(
            lambda _t: _coerce_node_input(verify_fn(source_node.get())),
        )(trigger_node)

        def _on_verified(value: Any) -> None:
            with batch():
                verified_node.down([(MessageType.DATA, value)])
                # V0 backfill: stamp which source version was verified (§6.0b).
                if has_source_versioning:
                    sv = source_node.v
                    if sv is not None:
                        verified_node.meta["source_version"].down(
                            [(MessageType.DATA, {"id": sv.id, "version": sv.version})]
                        )

        for_each(
            verify_stream,
            _on_verified,
            on_error=lambda _err: None,
        )

    return VerifiableBundle(node=source_node, verified=verified_node, trigger=trigger_node)


@dataclass(frozen=True, slots=True)
class Extraction:
    """Shape consumed by :func:`distill` extraction stages."""

    upsert: list[dict[str, Any]]
    remove: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CompactEntry:
    """Single entry in a :attr:`DistillBundle.compact` snapshot."""

    key: str
    value: Any
    score: float


@dataclass(frozen=True, slots=True)
class DistillBundle:
    """Result of :func:`distill`."""

    store: ReactiveMapBundle
    compact: Node[list[CompactEntry]]
    size: Node[int]


def _snapshot_map(store: ReactiveMapBundle) -> Mapping[str, Any]:
    snap = store.data.get()
    if snap is None:
        return {}
    if hasattr(snap, "value"):
        return cast("Mapping[str, Any]", snap.value)
    return cast("Mapping[str, Any]", snap)


def _apply_extraction(store: ReactiveMapBundle, extraction: Extraction) -> None:
    if not isinstance(extraction.upsert, list):
        msg = "distill extraction requires upsert: list[{key,value}]"
        raise TypeError(msg)
    with batch():
        for row in extraction.upsert:
            store.set(row["key"], row["value"])
        for key in extraction.remove:
            store.delete(key)


def distill(
    source: Any,
    extract_fn: Callable[[Any, Mapping[str, Any]], Any],
    *,
    score: Callable[[Any, Any], float],
    cost: Callable[[Any], float],
    budget: float = 2000,
    evict: Callable[[str, Any], Any] | None = None,
    consolidate: Callable[[Mapping[str, Any]], Any] | None = None,
    consolidate_trigger: Any | None = None,
    context: Any | None = None,
    map_options: dict[str, Any] | None = None,
    map_name: str | None = None,
    map_default_ttl: float | None = None,
    map_max_size: int | None = None,
) -> DistillBundle:
    """Budget-constrained reactive memory composition.

    Args:
        source: Source stream to distill.
        extract_fn: `(raw, existing) -> Extraction` as `NodeInput`.
        score: Relevance function for compact packing.
        cost: Cost function for compact packing.
        budget: Maximum compact budget (default `2000`).
        evict: Optional reactive eviction predicate per key.
        consolidate: Optional consolidation function.
        consolidate_trigger: Optional trigger for consolidation.
        context: Optional context source affecting compact ranking.
        map_options: Optional dict-style map config parity with TS (`name`,
            `default_ttl`, `max_size`).
        map_name: Optional underlying map node name.
        map_default_ttl: Optional default TTL seconds for underlying map.
        map_max_size: Optional underlying map max size.
    """
    source_node = from_any(source)
    context_node = from_any(context) if context is not None else state(None)
    resolved_default_ttl = (
        map_options["default_ttl"]
        if map_options and "default_ttl" in map_options
        else map_default_ttl
    )
    resolved_max_size = (
        map_options["max_size"] if map_options and "max_size" in map_options else map_max_size
    )
    resolved_name = map_options["name"] if map_options and "name" in map_options else map_name
    store = reactive_map(
        default_ttl=resolved_default_ttl,
        max_size=resolved_max_size,
        name=resolved_name,
    )

    extraction_stream = switch_map(
        lambda raw: _coerce_node_input(extract_fn(raw, _snapshot_map(store))),
    )(source_node)
    for_each(
        extraction_stream,
        lambda ex: _apply_extraction(store, ex),
        on_error=lambda _err: None,
    )

    if evict is not None:
        eviction_keys = dynamic_node(
            lambda get: _compute_evictions(get, store, evict),
        )

        def _delete_keys(keys: list[str]) -> None:
            for key in keys:
                store.delete(key)

        for_each(
            cast("Node[Any]", eviction_keys),
            _delete_keys,
            on_error=lambda _err: None,
        )

    if consolidate is not None and consolidate_trigger is not None:
        consolidation_stream = switch_map(
            lambda _t: _coerce_node_input(consolidate(_snapshot_map(store))),
        )(from_any(consolidate_trigger))
        for_each(
            consolidation_stream,
            lambda ex: _apply_extraction(store, ex),
            on_error=lambda _err: None,
        )

    compact = derived(
        [store.data, context_node],
        lambda deps, _a: _pack_compact(
            deps[0].value if hasattr(deps[0], "value") else deps[0],
            deps[1],
            score,
            cost,
            budget,
        ),
    )
    size = derived(
        [store.data],
        lambda deps, _a: len(deps[0].value if hasattr(deps[0], "value") else deps[0]),
        initial=0,
    )
    compact.subscribe(lambda _msgs: None)
    size.subscribe(lambda _msgs: None)

    return DistillBundle(store=store, compact=compact, size=size)


def _coerce_node_input(value: Any) -> Node[Any]:
    if isinstance(value, Node):
        return value
    if isinstance(value, AsyncIterable):
        return from_any(value)
    if isinstance(value, Awaitable):
        return from_any(value)
    if isinstance(value, Mapping):
        return of(value)
    if isinstance(value, (str, bytes, bytearray)):
        return of(value)
    if isinstance(value, Iterable):
        return from_any(value)
    return of(value)


def _compute_evictions(
    get: Callable[[Node[Any]], Any],
    store: ReactiveMapBundle,
    evict: Callable[[str, Any], Any],
) -> list[str]:
    out: list[str] = []
    current = get(store.data)
    snapshot = current.value if hasattr(current, "value") else current
    for key, mem in snapshot.items():
        verdict = evict(key, mem)
        if isinstance(verdict, Node):
            if get(verdict) is True:
                out.append(key)
            continue
        if isinstance(verdict, bool):
            if verdict:
                out.append(key)
            continue
        msg = "distill evict() must return bool or Node[bool] for reactive tracking"
        raise TypeError(msg)
    return out


def _pack_compact(
    snapshot: Mapping[str, Any],
    context: Any,
    score: Callable[[Any, Any], float],
    cost: Callable[[Any], float],
    budget: float,
) -> list[CompactEntry]:
    ranked = [
        {
            "key": key,
            "value": value,
            "score": float(score(value, context)),
            "cost": float(cost(value)),
        }
        for key, value in snapshot.items()
    ]
    ranked.sort(key=lambda row: row["score"], reverse=True)
    packed: list[CompactEntry] = []
    remaining = float(budget)
    for row in ranked:
        if row["cost"] <= remaining:
            packed.append(CompactEntry(key=row["key"], value=row["value"], score=row["score"]))
            remaining -= row["cost"]
    return packed


__all__ = [
    "CompactEntry",
    "DistillBundle",
    "Extraction",
    "VerifiableBundle",
    "distill",
    "verifiable",
]
