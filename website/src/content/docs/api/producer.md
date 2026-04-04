---
title: 'producer'
description: 'Create an auto-starting producer node with no dependencies.'
---

Create an auto-starting producer node with no dependencies.

## Signature

```python
def producer(fn: NodeFn, **opts: Any) -> Node[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `fn` | The compute function invoked when the first sink subscribes. |
| `opts` | Additional node options passed through to :func:`~graphrefly.core.node.node`. |

## Returns

A :class:`~graphrefly.core.node.Node` whose producer starts on first subscribe.

## Basic Usage

```python
from graphrefly import producer
from graphrefly.core.protocol import MessageType

def ticker(deps, actions):
    actions.emit(42)
    actions.down([(MessageType.COMPLETE,)])
    return lambda: None

p = producer(ticker, name="once")
```
