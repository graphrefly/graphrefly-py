---
title: 'distill'
description: 'Budget-constrained reactive memory composition.'
---

Budget-constrained reactive memory composition.

## Signature

```python
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
) -> DistillBundle
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `source` | Source stream to distill. |
| `extract_fn` | `(raw, existing) -&gt; Extraction` as `NodeInput`. |
| `score` | Relevance function for compact packing. |
| `cost` | Cost function for compact packing. |
| `budget` | Maximum compact budget (default `2000`). |
| `evict` | Optional reactive eviction predicate per key. |
| `consolidate` | Optional consolidation function. |
| `consolidate_trigger` | Optional trigger for consolidation. |
| `context` | Optional context source affecting compact ranking. |
| `map_options` | Optional dict-style map config parity with TS (`name`, `default_ttl`, `max_size`). |
| `map_name` | Optional underlying map node name. |
| `map_default_ttl` | Optional default TTL seconds for underlying map. |
| `map_max_size` | Optional underlying map max size. |
