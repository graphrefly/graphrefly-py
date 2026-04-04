---
title: 'concat'
description: 'Play ``first`` to completion, then continue with ``second``.'
---

Play ``first`` to completion, then continue with ``second``.

While ``first`` is active, ``DATA`` from ``second`` is buffered and replayed at handoff.

## Signature

```python
def concat(second: Node[Any]) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `second` | Segment played after the primary completes. |

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator`.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import concat as grf_cat
n = pipe(state(1), grf_cat(state(2)))
```
