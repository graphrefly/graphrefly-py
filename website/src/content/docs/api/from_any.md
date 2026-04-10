---
title: 'from_any'
description: 'Coerce a value into a :class:`~graphrefly.core.node.Node` using the best matching source.'
---

Coerce a value into a :class:`~graphrefly.core.node.Node` using the best matching source.

Dispatch rules:

- Existing :class:`~graphrefly.core.node.Node` -&gt; returned as-is.
- :class:`collections.abc.AsyncIterable` / async iterator -&gt; :func:`from_async_iter`.
- :class:`collections.abc.Awaitable` (incl. coroutines, futures) -&gt; :func:`from_awaitable`.
- Otherwise tries ``iter(value)``; if that fails uses :func:`of`.

## Signature

```python
def from_any(value: Any, *, runner: Any | None = None) -> Node[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `value` | Any value to coerce. |
| `runner` | Optional :class:`~graphrefly.core.runner.Runner` forwarded to :func:`from_awaitable` / :func:`from_async_iter` when applicable. |

## Returns

A :class:`~graphrefly.core.node.Node` wrapping *value*.

## Basic Usage

```python
from graphrefly.extra.sources import from_any
n = from_any([1, 2, 3])
from graphrefly.extra.sources import to_list
assert to_list(n) == [1, 2, 3]
```
