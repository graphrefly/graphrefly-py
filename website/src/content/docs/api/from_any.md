---
title: 'from_any'
description: 'Coerce *value* into a single-root :class:`~graphrefly.core.node.Node`.'
---

Coerce *value* into a single-root :class:`~graphrefly.core.node.Node`.

## Signature

```python
def from_any(value: Any) -> Node[Any]
```

## Documentation

Coerce *value* into a single-root :class:`~graphrefly.core.node.Node`.

- Existing :class:`~graphrefly.core.node.Node` → returned as-is
- :class:`collections.abc.AsyncIterable` / async iterator → :func:`from_async_iter`
- Awaitable / :class:`asyncio.Future` / coroutine → :func:`from_awaitable`
- Otherwise → :func:`from_iter` if ``iter(value)`` works (including ``str``), else :func:`of`
