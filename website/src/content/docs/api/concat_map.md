---
title: 'concat_map'
description: 'Map outer values to inner nodes; run inners strictly one after another.'
---

Map outer values to inner nodes; run inners strictly one after another.

While an inner is active, outer ``DATA`` values are queued. ``max_buffer &gt; 0`` drops the
oldest queued value when the queue would exceed that length.

## Signature

```python
def concat_map(
    fn: Callable[[Any], Any],
    *,
    initial: Any = _UNSET,
    max_buffer: int = 0,
) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `fn` | ``outer_value -&gt; source`` (coerced via :func:`graphrefly.extra.sources.from_any`). |
| `initial` | Optional initial ``get()`` value. |
| `max_buffer` | Maximum queued outer keys (``0`` = unlimited). |

## Returns

A unary pipe operator ``(Node) -&gt; Node``.

## Basic Usage

```python
from graphrefly import state, pipe
from graphrefly.extra.tier2 import concat_map
from graphrefly.extra import of
src = state(1)
out = pipe(src, concat_map(lambda v: of(v, v + 1)))
```
