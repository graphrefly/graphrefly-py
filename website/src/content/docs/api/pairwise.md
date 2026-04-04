---
title: 'pairwise'
description: 'Emit ``(previous, current)`` pairs; the first upstream value yields ``RESOLVED`` only.'
---

Emit ``(previous, current)`` pairs; the first upstream value yields ``RESOLVED`` only.

## Signature

```python
def pairwise() -> PipeOperator
```

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator`.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import pairwise as grf_pw
n = pipe(state(0), grf_pw())
```
