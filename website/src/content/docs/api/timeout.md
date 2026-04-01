---
title: 'timeout'
description: 'Emit ``ERROR`` if no ``DATA`` arrives within ``seconds`` after subscribe or last ``DATA``.'
---

Emit ``ERROR`` if no ``DATA`` arrives within ``seconds`` after subscribe or last ``DATA``.

## Signature

```python
def timeout(seconds: float, *, error: BaseException | None = None) -> PipeOperator
```

## Documentation

Emit ``ERROR`` if no ``DATA`` arrives within ``seconds`` after subscribe or last ``DATA``.

Timer resets on each ``DATA``; unsubscribe cancels the watchdog.

Args:
    seconds: Timeout window in seconds.
    error: Exception to send as the ``ERROR`` payload (default: :exc:`TimeoutError`).

Returns:
    A unary pipe operator ``(Node) -&gt; Node``.

Example:
    ```python
    from graphrefly.extra.tier2 import timeout
    from graphrefly.extra import never
    from graphrefly.extra.sources import first_value_from
    n = timeout(0.001)(never())
    try:
        first_value_from(n)
    except Exception:
        pass
    ```
