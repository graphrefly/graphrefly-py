---
title: 'with_breaker'
description: 'Guard a source node with a :class:`CircuitBreaker`.'
---

Guard a source node with a :class:`CircuitBreaker`.

## Signature

```python
def with_breaker(
    breaker: CircuitBreaker,
    *,
    on_open: Literal["skip", "error"] = "skip",
) -> Callable[[Node[Any]], WithBreakerBundle]
```

## Documentation

Guard a source node with a :class:`CircuitBreaker`.

On each upstream ``DATA``, if the breaker refuses work, either emit
``RESOLVED`` (``on_open="skip"``) or ``ERROR`` with :exc:`CircuitOpenError`
(``on_open="error"``). ``COMPLETE`` records success; ``ERROR`` records
failure and is forwarded. The ``breaker_state`` companion is wired into
``node.meta`` so it appears in ``describe()``.

Args:
    breaker: A :class:`CircuitBreaker` instance (see :func:`circuit_breaker`).
    on_open: ``"skip"`` (emit ``RESOLVED``) or ``"error"`` (emit
        :exc:`CircuitOpenError`) when the circuit is open.

Returns:
    A unary operator ``(Node) -&gt; WithBreakerBundle``.

Example:
    ```python
    from graphrefly import state
    from graphrefly.extra.resilience import circuit_breaker, with_breaker
    breaker = circuit_breaker(failure_threshold=3)
    src = state(1)
    bundle = with_breaker(breaker)(src)
    ```
