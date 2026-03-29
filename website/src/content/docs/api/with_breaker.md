---
title: 'with_breaker'
description: 'Guard a source with a :class:`CircuitBreaker`.'
---

Guard a source with a :class:`CircuitBreaker`.

## Signature

```python
def with_breaker(
    breaker: CircuitBreaker,
    *,
    on_open: Literal["skip", "error"] = "skip",
) -> Callable[[Node[Any]], WithBreakerBundle]
```

## Documentation

Guard a source with a :class:`CircuitBreaker`.

On each upstream ``DATA``, if the breaker refuses work, either emit ``RESOLVED`` (*skip*)
or ``ERROR`` (:exc:`CircuitOpenError`) (*error*). ``COMPLETE`` records success; ``ERROR``
records failure and is forwarded.
