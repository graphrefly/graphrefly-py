---
title: 'find'
description: 'Emit the first value satisfying ``predicate``, then ``COMPLETE``.'
---

Emit the first value satisfying ``predicate``, then ``COMPLETE``.

## Signature

```python
def find(predicate: Callable[[Any], bool]) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `predicate` | Match test. |

## Returns

A unary callable ``(Node) -&gt; Node`` composed from ``filter`` and ``take(1)``.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import find as grf_find
n = pipe(state(1), grf_find(lambda x: x > 0))
```
