---
title: 'start_with'
description: 'Emit ``value`` as ``DATA`` first, then forward every value from the source.'
---

Emit ``value`` as ``DATA`` first, then forward every value from the source.

## Signature

```python
def start_with(value: Any) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `value` | Prepended emission before upstream values. |

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator`.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import start_with as grf_sw
n = pipe(state(2), grf_sw(0))
```
