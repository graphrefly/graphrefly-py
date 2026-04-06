---
title: 'token_tracker'
description: 'Create a token bucket (alias for :func:`token_bucket`, naming parity with graphrefly-ts).'
---

Create a token bucket (alias for :func:`token_bucket`, naming parity with graphrefly-ts).

## Signature

```python
def token_tracker(capacity: float, refill_per_second: float) -> TokenBucket
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `capacity` | Maximum number of tokens. |
| `refill_per_second` | Tokens restored per second (``0`` = no refill). |

## Returns

A :class:`TokenBucket` instance.

## Basic Usage

```python
from graphrefly.extra.resilience import token_tracker
tb = token_tracker(10, 2.0)
assert tb.try_consume(1)
```
