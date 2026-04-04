---
title: 'merge'
description: 'Merge ``DATA`` from any dependency; ``COMPLETE`` only after every source completes.'
---

Merge ``DATA`` from any dependency; ``COMPLETE`` only after every source completes.

## Signature

```python
def merge(*sources: Node[Any]) -> Node[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `sources` | Upstreams to merge (empty → immediate ``COMPLETE`` node). |

## Returns

A :class:`~graphrefly.core.node.Node`.

## Basic Usage

```python
from graphrefly.extra import merge
from graphrefly import state
n = merge(state(1), state(2))
```
