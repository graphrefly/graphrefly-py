---
title: 'map'
description: 'Map each upstream settled value through ``fn``.'
---

Map each upstream settled value through ``fn``.

## Signature

```python
def map(
    fn: Callable[[Any], Any],
    *,
    equals: Callable[[Any, Any], bool] | None = None,
) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `fn` | Transform applied to each dependency value. |
| `equals` | Optional equality for ``RESOLVED`` detection on the inner node. |

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator` wrapping the upstream node.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import map as grf_map
n = pipe(state(1), grf_map(lambda x: x * 2))
```
