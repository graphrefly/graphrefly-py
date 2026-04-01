---
title: 'audit'
description: 'Emit the latest upstream value after ``seconds`` of trailing silence (Rx ``auditTime``).'
---

Emit the latest upstream value after ``seconds`` of trailing silence (Rx ``auditTime``).

## Signature

```python
def audit(seconds: float) -> PipeOperator
```

## Documentation

Emit the latest upstream value after ``seconds`` of trailing silence (Rx ``auditTime``).

Each ``DATA`` stores the latest value and restarts the timer. When the timer fires,
the stored value is emitted. No leading-edge emission.

Args:
    seconds: Trailing window duration in seconds.

Returns:
    A unary pipe operator ``(Node) -&gt; Node``.

Example:
    ```python
    from graphrefly import state, pipe
    from graphrefly.extra.tier2 import audit
    src = state(0)
    out = pipe(src, audit(0.05))
    ```
