---
title: 'exponential'
description: 'Create an exponential backoff strategy capped at ``max_delay_ns``.'
---

Create an exponential backoff strategy capped at ``max_delay_ns``.

## Signature

```python
def exponential(
    *,
    base_ns: int = 100_000_000,
    factor: float = 2.0,
    max_delay_ns: int = 30_000_000_000,
    jitter: JitterMode = "none",
) -> BackoffStrategy
```

## Documentation

Create an exponential backoff strategy capped at ``max_delay_ns``.

Args:
    base_ns: Initial delay in nanoseconds (default ``100_000_000`` = 100 ms).
    factor: Multiplicative growth factor &gt;= 1.0 (default ``2.0``).
    max_delay_ns: Upper bound on delay in nanoseconds (default ``30_000_000_000`` = 30 s).
    jitter: Jitter mode: ``"none"`` (default), ``"full"``, or ``"equal"``.

Returns:
    A :data:`BackoffStrategy` callable.

Example:
    ```python
    from graphrefly.extra.backoff import exponential
    s = exponential(base_ns=100_000_000, factor=2.0, max_delay_ns=30_000_000_000)
    assert s(0, None, None) == 100_000_000
    assert s(1, None, None) == 200_000_000
    ```
