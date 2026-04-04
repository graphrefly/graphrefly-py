---
title: 'repeat'
description: 'Play the source to ``COMPLETE``, then re-subscribe, repeating ``times`` passes total.'
---

Play the source to ``COMPLETE``, then re-subscribe, repeating ``times`` passes total.

Each pass ends when the source emits ``COMPLETE``; the operator then
subscribes again until all passes have finished.

## Signature

```python
def repeat(times: int) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `times` | Total number of source passes to play through. |

## Returns

A unary pipe operator ``(Node) -&gt; Node``.

## Basic Usage

```python
from graphrefly.extra import of
from graphrefly.extra.tier2 import repeat
from graphrefly.extra.sources import to_list
assert to_list(repeat(3)(of(1))) == [1, 1, 1]
```
