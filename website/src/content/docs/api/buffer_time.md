---
title: 'buffer_time'
description: 'Emit a list of ``DATA`` values collected over each timed window of ``seconds``.'
---

Emit a list of ``DATA`` values collected over each timed window of ``seconds``.

## Signature

```python
def buffer_time(seconds: float) -> PipeOperator
```

## Documentation

Emit a list of ``DATA`` values collected over each timed window of ``seconds``.

Args:
    seconds: Window duration in seconds.

Returns:
    A unary pipe operator ``(Node) -&gt; Node[list]``.

Example:
    ```python
    from graphrefly import state, pipe
    from graphrefly.extra.tier2 import buffer_time
    src = state(0)
    out = pipe(src, buffer_time(0.1))
    ```
