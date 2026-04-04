---
title: 'distinct_until_changed'
description: 'Suppress consecutive duplicates using ``equals`` (default: ``operator.eq``).'
---

Suppress consecutive duplicates using ``equals`` (default: ``operator.eq``).

## Signature

```python
def distinct_until_changed(
    equals: Callable[[Any, Any], bool] | None = None,
) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `equals` | Optional binary equality for adjacent values. |

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator`.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import distinct_until_changed as grf_duc
n = pipe(state(1), grf_duc())
```
