---
title: 'timeout_node'
description: 'Emit ``ERROR(TimeoutError)`` if no ``DATA`` arrives within *timeout_ns*.'
---

Emit ``ERROR(TimeoutError)`` if no ``DATA`` arrives within *timeout_ns*.

Timer starts on subscription and resets on each ``DATA``.
``COMPLETE`` or ``ERROR`` from upstream cancel the timer.

## Signature

```python
def timeout(source: Node[Any], timeout_ns: int) -> Node[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `source` | The upstream :class:`~graphrefly.core.node.Node`. |
| `timeout_ns` | Timeout duration in nanoseconds. |

## Returns

A new :class:`~graphrefly.core.node.Node` with timeout logic.

## Basic Usage

```python
from graphrefly.extra.resilience import timeout
from graphrefly.extra.sources import never
n = timeout(never(), 50_000_000)
```
