---
title: 'with_latest_from'
description: 'When the primary source settles, emit ``(primary, latest_secondary)``.'
---

When the primary source settles, emit ``(primary, latest_secondary)``.

Updates from ``other`` alone refresh the cached secondary value but do not emit.

## Signature

```python
def with_latest_from(other: Node[Any]) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `other` | Secondary node whose latest value is paired on primary emissions. |

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator`.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import with_latest_from as grf_wlf
n = pipe(state(1), grf_wlf(state("x")))
```
