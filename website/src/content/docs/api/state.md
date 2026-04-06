---
title: 'state'
description: 'Create a manually-settable source node with a fixed initial value.'
---

Create a manually-settable source node with a fixed initial value.

## Signature

```python
def state(initial: Any, **opts: Any) -> Node[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `initial` | The initial cached value for the node. Because ``initial`` is provided, ``equals`` is called on the first ``down()`` emission — if the value matches ``initial``, the node emits ``RESOLVED`` instead of ``DATA`` (spec §2.5). |
| `opts` | Additional node options passed through to :func:`~graphrefly.core.node.node`. |

## Returns

A :class:`~graphrefly.core.node.Node` with no deps and no compute function.

## Basic Usage

```python
from graphrefly import state
counter = state(0, name="counter")
counter.down([("DATA", 1)])
assert counter.get() == 1
```
