---
title: 'fallback'
description: 'On upstream terminal ``ERROR``, substitute *fb*.'
---

On upstream terminal ``ERROR``, substitute *fb*.

If *fb* is a plain value, emit ``[(DATA, fb), (COMPLETE,)]``.
If *fb* is a :class:`~graphrefly.core.node.Node`, subscribe to it and
forward its messages.

All non-``ERROR`` messages pass through unchanged.

## Signature

```python
def fallback(source: Node[Any], fb: Any) -> Node[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `source` | The upstream :class:`~graphrefly.core.node.Node`. |
| `fb` | Fallback value or :class:`~graphrefly.core.node.Node`. |

## Returns

A new :class:`~graphrefly.core.node.Node` with fallback logic.

## Basic Usage

```python
from graphrefly.extra.resilience import fallback
from graphrefly.extra.sources import throw_error
n = fallback(throw_error(ValueError("boom")), 42)
```
