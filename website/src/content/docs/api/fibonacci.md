---
title: 'fibonacci'
description: 'Create a backoff strategy with Fibonacci-scaled delays.'
---

Create a backoff strategy with Fibonacci-scaled delays.

## Signature

```python
def fibonacci(base_ns: int = 100_000_000, *, max_delay_ns: int = 30_000_000_000) -> BackoffStrategy
```

## Documentation

Create a backoff strategy with Fibonacci-scaled delays.

Delays follow the sequence ``1, 2, 3, 5, 8, ... * base_ns``, capped at
``max_delay_ns``.

Args:
    base_ns: Multiplier in nanoseconds (default ``100_000_000`` = 100 ms).
    max_delay_ns: Upper bound in nanoseconds (default ``30_000_000_000`` = 30 s).

Returns:
    A :data:`BackoffStrategy` callable.

Example:
    ```python
    from graphrefly.extra.backoff import fibonacci, NS_PER_SEC
    s = fibonacci(1 * NS_PER_SEC)
    assert s(0, None, None) == 1_000_000_000  # 1 * 1s
    assert s(1, None, None) == 2_000_000_000  # 2 * 1s
    ```
