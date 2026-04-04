---
title: 'take_while'
description: 'Emit while ``predicate`` holds; on first false, ``COMPLETE``.'
---

Emit while ``predicate`` holds; on first false, ``COMPLETE``.

Predicate exceptions propagate via node-level error handling (spec §2.4).

## Signature

```python
def take_while(predicate: Callable[[Any], bool]) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `predicate` | Continuation test for each value. |

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator`.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import take_while as grf_tw
n = pipe(state(1), grf_tw(lambda x: x < 10))
```
