---
title: 'scan'
description: 'Fold upstream values with ``reducer(acc, value) -&gt; acc``; emit accumulator after each step.'
---

Fold upstream values with ``reducer(acc, value) -&gt; acc``; emit accumulator after each step.

Unlike RxJS, seed is always required — there is no seedless mode where the first value
silently becomes the accumulator.

## Signature

```python
def scan(
    reducer: Callable[[Any, Any], Any],
    seed: Any,
    *,
    equals: Callable[[Any, Any], bool] | None = None,
) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `reducer` | Accumulator update. |
| `seed` | Initial accumulator (also used for ``initial`` on the inner node). |
| `equals` | Optional equality for consecutive emissions (default ``operator.eq``). |

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator`.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import scan as grf_scan
n = pipe(state(1), grf_scan(lambda a, x: a + x, 0))
```
