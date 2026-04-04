---
title: 'share'
description: 'Share one upstream subscription across all downstream sinks (ref-counted).'
---

Share one upstream subscription across all downstream sinks (ref-counted).

## Signature

```python
def share[T](source: Node[T]) -> Node[T]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `source` | The upstream node to multicast. |

## Returns

A new :class:`~graphrefly.core.node.Node` that connects to *source* once
and ref-counts downstream subscriptions.

## Basic Usage

```python
from graphrefly import state
from graphrefly.extra.sources import share, for_each
x = state(0)
s = share(x)
log = []
unsub = for_each(s, log.append)
x.down([("DATA", 1)])
unsub()
```
