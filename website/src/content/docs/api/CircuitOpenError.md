---
title: 'CircuitOpenError'
description: 'Raised when ``with_breaker(..., on_open="error")`` sees an open circuit.'
---

Raised when ``with_breaker(..., on_open="error")`` sees an open circuit.

## Signature

```python
class CircuitOpenError(RuntimeError)
```

## Documentation

Raised when ``with_breaker(..., on_open="error")`` sees an open circuit.

Example:
    ```python
    from graphrefly.extra.resilience import CircuitOpenError, circuit_breaker, with_breaker
    from graphrefly import state
    from graphrefly.extra.sources import first_value_from
    breaker = circuit_breaker(failure_threshold=1)
    breaker.record_failure()  # open the circuit
    src = state(1)
    bundle = with_breaker(breaker, on_open="error")(src)
    # next DATA from src will raise CircuitOpenError
    ```
