---
title: 'buffer_count'
description: 'Emit a list of every ``n`` consecutive ``DATA`` values as a single emission.'
---

Emit a list of every ``n`` consecutive ``DATA`` values as a single emission.

## Signature

```python
def buffer_count(n: int) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `n` | Number of ``DATA`` values to collect per emitted list. |

## Returns

A unary pipe operator ``(Node) -&gt; Node[list]``.

## Basic Usage

```python
from graphrefly.extra import of
from graphrefly.extra.tier2 import buffer_count
from graphrefly.extra.sources import first_value_from
out = buffer_count(3)(of(1, 2, 3, 4))
assert first_value_from(out) == [1, 2, 3]
```
