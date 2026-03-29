---
title: 'reactive_map'
description: 'Creates a reactive key–value map with optional TTL and LRU eviction.'
---

Creates a reactive key–value map with optional TTL and LRU eviction.

## Signature

```python
def reactive_map(
    *,
    default_ttl: float | None = None,
    max_size: int | None = None,
    name: str | None = None,
) -> ReactiveMapBundle
```

## Documentation

Creates a reactive key–value map with optional TTL and LRU eviction.

Args:
    default_ttl: If set, seconds until expiry when :meth:`ReactiveMapBundle.set` omits
        ``ttl`` (``None`` = no default expiry).
    max_size: If set, maximum number of entries; evicts LRU when exceeded (must be &gt;= 1).
    name: Optional registry name for ``describe()`` / debugging.

Returns:
    A :class:`ReactiveMapBundle` with imperative ``set`` / ``delete`` / ``clear`` /
    ``prune`` and a ``data`` node exposing the live snapshot.

Examples:
    &gt;&gt;&gt; from graphrefly.extra import reactive_map
    &gt;&gt;&gt; m = reactive_map(default_ttl=60.0, max_size=100)
    &gt;&gt;&gt; m.set("x", 1)
    &gt;&gt;&gt; m.data.get().value["x"]
    1
