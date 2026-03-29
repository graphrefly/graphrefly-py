---
title: 'for_each'
description: 'Subscribe to *source* and invoke ``fn(value)`` for each ``DATA``.'
---

Subscribe to *source* and invoke ``fn(value)`` for each ``DATA``.

## Signature

```python
def for_each(
    source: Node[Any],
    fn: Callable[[Any], None],
    *,
    on_error: Callable[[BaseException], None] | None = None,
) -> Callable[[], None]
```

## Documentation

Subscribe to *source* and invoke ``fn(value)`` for each ``DATA``.

Returns an unsubscribe callable. Prefer ``on_error`` for ``ERROR`` handling; the default
path raises the error from inside the sink and may not always propagate through
:meth:`~graphrefly.core.node.Node.subscribe` when ``thread_safe`` is enabled.
