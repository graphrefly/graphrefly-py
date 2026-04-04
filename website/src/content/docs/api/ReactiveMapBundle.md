---
title: 'ReactiveMapBundle'
description: 'Key–value store with optional per-key TTL and LRU eviction at capacity.'
---

Key–value store with optional per-key TTL and LRU eviction at capacity.

Attributes:
    data: A :func:`~graphrefly.core.sugar.state` node whose value is a
        :class:`~types.MappingProxyType` (immutable) of non-expired keys.
        Uses versioned snapshots for efficient ``RESOLVED`` deduplication.

## Signature

```python
class ReactiveMapBundle
```

## Basic Usage

```python
from graphrefly.extra import reactive_map
m = reactive_map(max_size=10)
m.set("a", 1)
assert m.data.get().value["a"] == 1
```

## Notes

TTL deadlines use :func:`~graphrefly.core.clock.monotonic_ns`
(nanoseconds). Expired keys remain in the internal store until the
next mutation or :meth:`prune`. LRU order is updated on
``set``, not on dict reads from ``data``.
