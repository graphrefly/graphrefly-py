---
title: 'take'
description: 'Emit at most ``n`` wire ``DATA`` values, then ``COMPLETE`` (``RESOLVED`` does not count).'
---

Emit at most ``n`` wire ``DATA`` values, then ``COMPLETE`` (``RESOLVED`` does not count).

## Signature

```python
def take(n: int) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `n` | Maximum ``DATA`` emissions; ``n &lt;= 0`` completes immediately. |

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator`.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import take as grf_take
n = pipe(state(0), grf_take(3))
```
