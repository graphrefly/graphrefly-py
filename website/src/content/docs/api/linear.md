---
title: 'linear'
description: 'Create a backoff strategy with linearly increasing delay.'
---

Create a backoff strategy with linearly increasing delay.

## Signature

```python
def linear(base_ns: int, step_ns: int | None = None) -> BackoffStrategy
```

## Documentation

Create a backoff strategy with linearly increasing delay.

Delay is ``base_ns + step_ns * attempt`` where *step_ns* defaults to *base_ns*.

Args:
    base_ns: Starting delay in nanoseconds.
    step_ns: Increment per attempt in nanoseconds (defaults to *base_ns*).

Returns:
    A :data:`BackoffStrategy` callable.

Example:
    ```python
    from graphrefly.extra.backoff import linear, NS_PER_SEC
    s = linear(1 * NS_PER_SEC)
    assert s(0, None, None) == 1_000_000_000
    assert s(2, None, None) == 3_000_000_000
    ```
