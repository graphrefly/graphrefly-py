---
title: 'node'
description: 'Create a reactive node (mirrors graphrefly-ts ``node`` overloads).'
---

Create a reactive node (mirrors graphrefly-ts ``node`` overloads).

Accepts multiple call signatures::

    node()                           # no-dep, no-fn source
    node(fn)                         # producer (no deps)
    node([dep1, dep2], fn)           # derived / operator
    node([dep1], fn, {"name": ...})  # with options dict
    node(name="x", initial=0)        # kwargs shorthand

## Signature

```python
def node(
    deps_or_fn: Sequence[NodeImpl[Any]] | NodeFn | dict[str, Any] | None = None,
    fn_or_opts: NodeFn | dict[str, Any] | None = None,
    opts_arg: dict[str, Any] | None = None,
    **kwargs: Any,
) -> NodeImpl[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `deps_or_fn` | Either a sequence of upstream :class:`NodeImpl` dependencies, a bare compute function (producer), an options dict, or ``None``. |
| `fn_or_opts` | Compute function (when ``deps_or_fn`` is a dep list) or an options dict. |
| `opts_arg` | Additional options dict when both deps and fn are provided positionally. |
| `kwargs` | Any option key accepted by :class:`NodeImpl` (e.g. ``name``, ``initial``, ``equals``, ``guard``, ``thread_safe``). |

## Returns

A new :class:`NodeImpl` instance.

## Basic Usage

```python
from graphrefly import node, state
x = state(0)
doubled = node([x], lambda deps, _: deps[0] * 2, name="doubled")
```
