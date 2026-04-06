---
title: 'cache'
description: 'Memoize last ``DATA`` value with a TTL.'
---

Memoize last ``DATA`` value with a TTL.

On new subscriber, if a cached value exists within TTL, replay it
immediately then forward live messages.

## Signature

```python
def cache(source: Node[Any], ttl_ns: int) -> Node[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `source` | The upstream :class:`~graphrefly.core.node.Node`. |
| `ttl_ns` | Time-to-live in nanoseconds for the cached value. |

## Returns

A new :class:`~graphrefly.core.node.Node` with caching logic.

## Basic Usage

```python
from graphrefly.extra.resilience import cache
from graphrefly.extra.sources import of
n = cache(of(1), 60_000_000_000)
```
