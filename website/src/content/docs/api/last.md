---
title: 'last'
description: 'Buffer ``DATA``; on ``COMPLETE``, emit the final value (or ``default``).'
---

Buffer ``DATA``; on ``COMPLETE``, emit the final value (or ``default``).

If no ``default`` is given and the source completes without emitting, only ``COMPLETE``
is forwarded (no ``DATA``).

## Signature

```python
def last(*, default: Any = _LAST_NO_DEFAULT) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `default` | Optional value emitted when the source completes empty. |

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator`.

## Basic Usage

```python
from graphrefly import pipe, state
from graphrefly.extra import last as grf_last
n = pipe(state(1), grf_last(default=0))
```
