---
title: 'first'
description: 'Emit the first ``DATA`` then ``COMPLETE`` (same as ``take(1)``).'
---

Emit the first ``DATA`` then ``COMPLETE`` (same as ``take(1)``).

## Signature

```python
def first() -> PipeOperator
```

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator`.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import first as grf_first
n = pipe(state(42), grf_first())
```
