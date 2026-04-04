---
title: 'skip'
description: 'Drop the first ``n`` wire ``DATA`` payloads (``RESOLVED`` does not advance the counter).'
---

Drop the first ``n`` wire ``DATA`` payloads (``RESOLVED`` does not advance the counter).

## Signature

```python
def skip(n: int) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `n` | Number of ``DATA`` values to suppress before forwarding. |

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator`.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import skip as grf_skip
n = pipe(state(0), grf_skip(2))
```
