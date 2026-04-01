---
title: 'delay'
description: 'Delay each ``DATA`` message by ``seconds`` (one timer per pending value, FIFO order).'
---

Delay each ``DATA`` message by ``seconds`` (one timer per pending value, FIFO order).

## Signature

```python
def delay(seconds: float) -> PipeOperator
```

## Documentation

Delay each ``DATA`` message by ``seconds`` (one timer per pending value, FIFO order).

Args:
    seconds: Delay in seconds applied to each ``DATA`` message.

Returns:
    A unary pipe operator ``(Node) -&gt; Node``.

Example:
    ```python
    from graphrefly import state, pipe
    from graphrefly.extra.tier2 import delay
    src = state(0)
    out = pipe(src, delay(0.01))
    ```
