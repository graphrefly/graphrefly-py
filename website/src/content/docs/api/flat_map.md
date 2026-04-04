---
title: 'flat_map'
description: 'Map each outer value to an inner node; subscribe to every inner concurrently (merge).'
---

Map each outer value to an inner node; subscribe to every inner concurrently (merge).

Completes when the outer has completed and every inner subscription has ended.

## Signature

```python
def flat_map(
    fn: Callable[[Any], Any],
    *,
    initial: Any = _UNSET,
    concurrent: int | None = None,
) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `fn` | ``outer_value -&gt; source`` (coerced via :func:`graphrefly.extra.sources.from_any`). |
| `initial` | Optional initial ``get()`` value. |
| `concurrent` | When set, limit the number of concurrently active inner subscriptions. Outer values beyond this limit are buffered and drained as inner subscriptions complete. |

## Returns

A unary pipe operator ``(Node) -&gt; Node``.

## Basic Usage

```python
from graphrefly import state, pipe
from graphrefly.extra.tier2 import flat_map
from graphrefly.extra import of
src = state(1)
out = pipe(src, flat_map(lambda v: of(v * 2)))
```
