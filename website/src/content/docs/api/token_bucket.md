---
title: 'token_bucket'
description: 'Factory for a thread-safe :class:`TokenBucket`.'
---

Factory for a thread-safe :class:`TokenBucket`.

## Signature

```python
def token_bucket(capacity: float, refill_per_second: float) -> TokenBucket
```

## Documentation

Factory for a thread-safe :class:`TokenBucket`.

Args:
    capacity: Maximum tokens.
    refill_per_second: Tokens restored per second (``0`` = no refill).
