---
title: 'reduce'
description: 'Reduce to one value emitted when the source completes.'
---

Reduce to one value emitted when the source completes.

Unlike RxJS, seed is always required. If the source completes without emitting DATA,
the seed value is emitted (RxJS would throw without a seed).

On an empty completion (no prior ``DATA``), emits ``seed``.

## Signature

```python
def reduce(  # noqa: A001 — roadmap API name
    reducer: Callable[[Any, Any], Any],
    seed: Any,
) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `reducer` | Accumulator update (return value is not used until completion). |
| `seed` | Value used when the source completes with no prior ``DATA``. |

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator`.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import reduce as grf_reduce
n = pipe(state(1), grf_reduce(lambda a, x: a + x, 0))
```
