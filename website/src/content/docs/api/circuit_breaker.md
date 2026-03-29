---
title: 'circuit_breaker'
description: 'Thread-safe circuit breaker (closed / open / half-open).'
---

Thread-safe circuit breaker (closed / open / half-open).

## Signature

```python
def circuit_breaker(
    *,
    failure_threshold: int = 5,
    cooldown: float = 30.0,
    cooldown_strategy: BackoffStrategy | None = None,
    half_open_max: int = 1,
) -> CircuitBreaker
```

## Documentation

Thread-safe circuit breaker (closed / open / half-open).

Supports escalating cooldown via an optional backoff strategy.

Args:
    failure_threshold: Failures before opening (default ``5``).
    cooldown: Base cooldown seconds (default ``30.0``).
    cooldown_strategy: Backoff for cooldown escalation.
    half_open_max: Trials in half-open (default ``1``).
