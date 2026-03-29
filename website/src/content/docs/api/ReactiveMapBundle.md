---
title: 'ReactiveMapBundle'
description: 'Key–value store with optional per-key TTL and LRU eviction at capacity.'
---

Key–value store with optional per-key TTL and LRU eviction at capacity.

## Signature

```python
class ReactiveMapBundle
```

## Documentation

Key–value store with optional per-key TTL and LRU eviction at capacity.

Attributes:
    data: A :func:`~graphrefly.core.sugar.state` node whose value is a
        :class:`~types.MappingProxyType` (immutable) of non-expired keys.
        Uses versioned snapshots for efficient ``RESOLVED`` deduplication.

Notes:
    TTL deadlines use :func:`time.monotonic` (seconds). Expired keys remain in the
    internal store until the next mutation or :meth:`prune`. LRU order is updated on
    ``set``, not on dict reads from ``data``.
