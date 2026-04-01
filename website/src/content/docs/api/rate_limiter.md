---
title: 'rate_limiter'
description: 'Limit upstream ``DATA`` to at most *max_events* per *window_ns* (sliding window).'
---

Limit upstream ``DATA`` to at most *max_events* per *window_ns* (sliding window).

## Signature

```python
def rate_limiter(source: Node[Any], max_events: int, window_ns: int) -> Node[Any]
```

## Documentation

Limit upstream ``DATA`` to at most *max_events* per *window_ns* (sliding window).

Values exceeding the budget are queued (FIFO) and emitted as slots free.
``DIRTY`` and ``RESOLVED`` still propagate immediately when not blocked.

Args:
    source: The upstream :class:`~graphrefly.core.node.Node` to rate-limit.
    max_events: Maximum ``DATA`` emissions allowed per window.
    window_ns: Window duration in nanoseconds.

Returns:
    A new :class:`~graphrefly.core.node.Node` with rate-limiting applied.

Example:
    ```python
    from graphrefly import state
    from graphrefly.extra.resilience import rate_limiter
    from graphrefly.extra.sources import to_list
    from graphrefly.extra import of
    n = rate_limiter(of(1, 2, 3), max_events=2, window_ns=60_000_000_000)
    ```
