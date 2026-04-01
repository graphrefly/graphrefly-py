---
title: 'from_any'
description: 'Coerce a value into a :class:`~graphrefly.core.node.Node` using the best matching source.'
---

Coerce a value into a :class:`~graphrefly.core.node.Node` using the best matching source.

## Signature

```python
def from_any(value: Any) -> Node[Any]
```

## Documentation

Coerce a value into a :class:`~graphrefly.core.node.Node` using the best matching source.

Dispatch rules:

- Existing :class:`~graphrefly.core.node.Node` → returned as-is.
- :class:`collections.abc.AsyncIterable` / async iterator → :func:`from_async_iter`.
- Awaitable / :class:`asyncio.Future` / coroutine → :func:`from_awaitable`.
- Otherwise tries ``iter(value)``; if that fails uses :func:`of`.

Args:
    value: Any value to coerce.

Returns:
    A :class:`~graphrefly.core.node.Node` wrapping *value*.

Example:
    ```python
    from graphrefly.extra.sources import from_any
    n = from_any([1, 2, 3])
    from graphrefly.extra.sources import to_list
    assert to_list(n) == [1, 2, 3]
    ```
