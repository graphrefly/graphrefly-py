---
title: 'token_tracker'
description: 'Create a token bucket (alias for :func:`token_bucket`, kept for backward compat).'
---

Create a token bucket (alias for :func:`token_bucket`, kept for backward compat).

## Signature

```python
def token_tracker(capacity: float, refill_per_second: float) -> TokenBucket
```

## Documentation

Create a token bucket (alias for :func:`token_bucket`, kept for backward compat).

Args:
    capacity: Maximum number of tokens.
    refill_per_second: Tokens restored per second (``0`` = no refill).

Returns:
    A :class:`TokenBucket` instance.

Example:
    ```python
    from graphrefly.extra.resilience import token_tracker
    tb = token_tracker(10, 2.0)
    assert tb.try_consume(1)
    ```
