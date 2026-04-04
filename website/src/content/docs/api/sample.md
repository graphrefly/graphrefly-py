---
title: 'sample'
description: "Emit the primary's latest value whenever ``notifier`` settles with ``DATA``."
---

Emit the primary's latest value whenever ``notifier`` settles with ``DATA``.

Source messages are intercepted via ``on_message``; only notifier ``DATA``
(dep index 1) triggers ``src.get()`` emission. Matches TS ``sample`` architecture.

## Signature

```python
def sample(notifier: Node[Any]) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `notifier` | Node whose ``DATA`` triggers sampling of the primary's latest value. |

## Returns

A unary pipe operator ``(Node) -&gt; Node``.

## Basic Usage

```python
from graphrefly import state, pipe
from graphrefly.extra.tier2 import sample
src = state(0)
tick = state(None)
out = pipe(src, sample(tick))
```
