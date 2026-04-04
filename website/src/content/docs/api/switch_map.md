---
title: 'switch_map'
description: 'Map each outer settled value to an inner node; keep only the latest inner subscription.'
---

Map each outer settled value to an inner node; keep only the latest inner subscription.

On each outer ``DATA``, the previous inner is unsubscribed. Inner ``DATA`` / ``RESOLVED`` /
``DIRTY`` are forwarded; inner ``ERROR`` always terminates; inner ``COMPLETE`` completes
the output only if the outer has already completed.

## Signature

```python
def switch_map(
    fn: Callable[[Any], Any],
    *,
    initial: Any = _UNSET,
) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `fn` | ``outer_value -&gt; source`` for the active inner. Return ``Node``, scalar, awaitable, iterable, or async iterable (coerced via :func:`graphrefly.extra.sources.from_any`). |
| `initial` | Optional seed for :meth:`~graphrefly.core.node.Node.get` before the first inner emission. |

## Returns

A unary pipe operator ``(Node) -&gt; Node``.

## Basic Usage

```python
from graphrefly import state, pipe
from graphrefly.extra.tier2 import switch_map
from graphrefly.extra import of
src = state(1)
out = pipe(src, switch_map(lambda v: of(v * 10)))
```
