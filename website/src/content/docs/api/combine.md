---
title: 'combine'
description: 'Combine latest values from all sources into a tuple whenever any settles.'
---

Combine latest values from all sources into a tuple whenever any settles.

## Signature

```python
def combine(*sources: Node[Any]) -> Node[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `sources` | Upstream nodes (empty → empty tuple node). |

## Returns

A :class:`~graphrefly.core.node.Node` emitting ``tuple`` of dependency values.

## Basic Usage

```python
from graphrefly.extra import combine
from graphrefly import state
n = combine(state(1), state("a"))
```
