---
title: 'from_timer'
description: 'Emit a value after a delay, then optionally tick at a fixed period (like Rx ``timer``).'
---

Emit a value after a delay, then optionally tick at a fixed period (like Rx ``timer``).

## Signature

```python
def from_timer(
    delay: float,
    period: float | None = None,
    *,
    first: int = 0,
) -> Node[Any]
```

## Documentation

Emit a value after a delay, then optionally tick at a fixed period (like Rx ``timer``).

If *period* is ``None``, emit *first* once then ``COMPLETE``. If *period* is
set, emit *first*, *first+1*, *first+2*, … every *period* seconds. Timer
threads are daemonized and cancelled on unsubscribe.

Args:
    delay: Seconds to wait before the first emission (must be &gt;= 0).
    period: Optional repeat interval in seconds (``None`` = one-shot).
    first: Integer value for the first emission (default ``0``).

Returns:
    A :class:`~graphrefly.core.node.Node` that emits on a timer thread.

Example:
    ```python
    from graphrefly.extra import from_timer
    from graphrefly.extra.sources import first_value_from
    n = from_timer(0.001)
    assert first_value_from(n) == 0
    ```
