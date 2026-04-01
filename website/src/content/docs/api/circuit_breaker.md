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
    cooldown_ns: int = 30_000_000_000,
    cooldown_strategy: BackoffStrategy | None = None,
    half_open_max: int = 1,
) -> CircuitBreaker
```

## Documentation

Thread-safe circuit breaker (closed / open / half-open).

Supports escalating cooldown via an optional backoff strategy.

Args:
    failure_threshold: Failures before opening (default ``5``).
    cooldown_ns: Base cooldown in nanoseconds (default ``30_000_000_000`` = 30 s).
    cooldown_strategy: Backoff for cooldown escalation.
    half_open_max: Trials in half-open (default ``1``).
