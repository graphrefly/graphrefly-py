---
title: 'empty'
description: 'Emit ``COMPLETE`` immediately when the first sink subscribes.'
---

Emit ``COMPLETE`` immediately when the first sink subscribes.

## Signature

```python
def empty() -> Node[Any]
```

## Returns

A :class:`~graphrefly.core.node.Node` that completes with no ``DATA``.

## Basic Usage

```python
from graphrefly.extra import empty
from graphrefly.extra.sources import to_list
assert to_list(empty()) == []
```
