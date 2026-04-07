---
title: 'valve'
description: 'Forward ``DATA`` only when ``control`` is truthy; otherwise emit ``RESOLVED``.'
---

Forward ``DATA`` only when ``control`` is truthy; otherwise emit ``RESOLVED``.

This is a value-level valve using a boolean control signal. See :func:`pausable`
for protocol-level ``PAUSE``/``RESUME`` buffering.

## Signature

```python
def valve(control: Node[Any]) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `control` | Boolean-valued node; ``True`` lets values through, ``False`` suppresses them. |

## Returns

A unary pipe operator ``(Node) -&gt; Node``.

## Basic Usage

```python
from graphrefly import state, pipe
from graphrefly.extra.tier2 import valve
src = state(1)
ctrl = state(True)
out = pipe(src, valve(ctrl))
```
