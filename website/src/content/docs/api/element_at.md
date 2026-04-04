---
title: 'element_at'
description: 'Emit the value at zero-based ``DATA`` index ``index``, then ``COMPLETE``.'
---

Emit the value at zero-based ``DATA`` index ``index``, then ``COMPLETE``.

## Signature

```python
def element_at(index: int) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `index` | Number of prior ``DATA`` emissions to skip. |

## Returns

A unary callable ``(Node) -&gt; Node`` composed from ``skip`` and ``take(1)``.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import element_at as grf_at
n = pipe(state(0), grf_at(2))
```
