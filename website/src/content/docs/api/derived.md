---
title: 'derived'
description: 'Create a derived node that recomputes whenever its dependencies settle.'
---

Create a derived node that recomputes whenever its dependencies settle.

## Signature

```python
def derived(deps: Sequence[Node[Any]], fn: NodeFn, **opts: Any) -> Node[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `deps` | Upstream nodes whose settled values are passed to ``fn``. |
| `fn` | Compute function receiving ``(dep_values, actions)``; may return a value to emit or a cleanup callable. |
| `opts` | Additional node options passed through to :func:`~graphrefly.core.node.node`. |

## Returns

A :class:`~graphrefly.core.node.Node` that reacts to its upstream deps.

## Basic Usage

```python
from graphrefly import state, derived
x = state(2)
doubled = derived([x], lambda deps, _: deps[0] * 2)
```
