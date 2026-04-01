---
title: 'window_time'
description: 'Split source ``DATA`` into sub-node windows, each lasting ``seconds``.'
---

Split source ``DATA`` into sub-node windows, each lasting ``seconds``.

## Signature

```python
def window_time(seconds: float) -> PipeOperator
```

## Documentation

Split source ``DATA`` into sub-node windows, each lasting ``seconds``.

Each emitted value is a :class:`~graphrefly.core.node.Node` receiving values
collected during that time window.

Args:
    seconds: Duration of each window in seconds.

Returns:
    A unary pipe operator ``(Node) -&gt; Node[Node]``.

Example:
    ```python
    from graphrefly import state, pipe
    from graphrefly.extra.tier2 import window_time
    src = state(0)
    out = pipe(src, window_time(0.1))
    ```
