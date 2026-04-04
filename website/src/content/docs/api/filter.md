---
title: 'filter'
description: 'Forward values where ``predicate`` is true; otherwise emit ``RESOLVED`` (no ``DATA``).'
---

Forward values where ``predicate`` is true; otherwise emit ``RESOLVED`` (no ``DATA``).

Pure predicate gate — no implicit dedup (use ``distinct_until_changed`` for that).

## Signature

```python
def filter(
    predicate: Callable[[Any], bool],
) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `predicate` | Inclusion test for each value. |

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator`.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import filter as grf_filter
n = pipe(state(1), grf_filter(lambda x: x > 0))
```
