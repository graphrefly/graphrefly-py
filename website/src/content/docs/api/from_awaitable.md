---
title: 'from_awaitable'
description: 'Resolve an awaitable via a :class:`~graphrefly.core.runner.Runner`.'
---

Resolve an awaitable via a :class:`~graphrefly.core.runner.Runner`.

## Signature

```python
def from_awaitable(
    awaitable: Awaitable[Any],
    *,
    runner: Any | None = None,
) -> Node[Any]
```

## Documentation

Resolve an awaitable via a :class:`~graphrefly.core.runner.Runner`.

Args:
    awaitable: The awaitable/coroutine to resolve.
    runner: Optional :class:`~graphrefly.core.runner.Runner`.  When ``None``,
        uses the thread-local default runner (see
        :func:`~graphrefly.core.runner.set_default_runner`).

Returns:
    A :class:`~graphrefly.core.node.Node` that emits one ``DATA`` then
    ``COMPLETE``, or ``ERROR`` on failure.
