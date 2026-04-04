---
title: 'SubscribeHints'
description: 'Hints passed to :meth:`~graphrefly.core.node.NodeImpl.subscribe` to enable optimizations.'
---

Hints passed to :meth:`~graphrefly.core.node.NodeImpl.subscribe` to enable optimizations.

## Signature

```python
class SubscribeHints
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `single_dep` | When ``True``, the subscribing node has exactly one dependency, enabling the single-dep fast path that skips redundant ``DIRTY`` messages. |

## Basic Usage

```python
from graphrefly import state
from graphrefly.core.node import SubscribeHints
x = state(1)
hints = SubscribeHints(single_dep=True)
unsub = x.subscribe(lambda msgs: None, hints)
```
