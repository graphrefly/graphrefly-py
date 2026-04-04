---
title: 'interval'
description: 'Create a producer that emits ``0, 1, 2, …`` at a fixed timer interval.'
---

Create a producer that emits ``0, 1, 2, …`` at a fixed timer interval.

## Signature

```python
def interval(seconds: float) -> Node[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `seconds` | Interval between emissions in seconds. |

## Returns

A :class:`~graphrefly.core.node.Node` that emits on a recurring timer thread.

## Basic Usage

```python
from graphrefly.extra.tier2 import interval
from graphrefly.extra.sources import first_value_from
n = interval(0.001)
assert first_value_from(n) == 0
```
