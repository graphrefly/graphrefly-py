---
title: 'exhaust_map'
description: 'Like :func:`switch_map`, but ignores new outer ``DATA`` while the current inner is active.'
---

Like :func:`switch_map`, but ignores new outer ``DATA`` while the current inner is active.

## Signature

```python
def exhaust_map(fn: Callable[[Any], Any], *, initial: Any = _UNSET) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `fn` | ``outer_value -&gt; source`` (coerced via :func:`graphrefly.extra.sources.from_any`). |
| `initial` | Optional initial ``get()`` value. |

## Returns

A unary pipe operator ``(Node) -&gt; Node``.

## Basic Usage

```python
from graphrefly import state, pipe
from graphrefly.extra.tier2 import exhaust_map
from graphrefly.extra import of
src = state(1)
out = pipe(src, exhaust_map(lambda v: of(v)))
```
