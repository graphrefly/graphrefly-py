---
title: 'effect'
description: 'Create a side-effect leaf node; ``fn`` should return ``None`` (no auto-emit).'
---

Create a side-effect leaf node; ``fn`` should return ``None`` (no auto-emit).

## Signature

```python
def effect(deps: Sequence[Node[Any]], fn: NodeFn, **opts: Any) -> Node[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `deps` | Upstream nodes whose settled values trigger ``fn``. |
| `fn` | Side-effect function receiving ``(dep_values, actions)``; return value is ignored unless it is a cleanup callable. |
| `opts` | Additional node options passed through to :func:`~graphrefly.core.node.node`. |

## Returns

A :class:`~graphrefly.core.node.Node` that runs ``fn`` on each settlement
but does not emit reactive values downstream.

## Basic Usage

```python
from graphrefly import state, effect
x = state(0)
log = []
e = effect([x], lambda deps, _: log.append(deps[0]))
x.down([("DATA", 1)])
```
