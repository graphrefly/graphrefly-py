---
title: 'ReactiveMapBundle'
description: 'Key–value store with optional per-key TTL and LRU eviction at capacity.'
---

Key–value store with optional per-key TTL and LRU eviction at capacity.

Attributes:
    entries: A :func:`~graphrefly.core.sugar.state` node whose value is a
        :class:`~types.MappingProxyType` (immutable) of non-expired keys.

## Signature

```python
class ReactiveMapBundle
```

## Basic Usage

```python
from graphrefly.extra import reactive_map
m = reactive_map(max_size=10)
m.set("a", 1)
assert m.get("a") == 1        # synchronous key lookup
assert m.has("a") is True
assert m.size == 1
```

## Notes

TTL deadlines use :func:`~graphrefly.core.clock.monotonic_ns`
(nanoseconds). Expired keys are evicted eagerly on ``get``/``has``
and lazily on mutations via :meth:`prune_expired`. LRU order is
refreshed on ``set``, ``get``, and ``has``.
