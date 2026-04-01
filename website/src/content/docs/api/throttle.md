---
title: 'throttle'
description: 'Rate-limit: emit at most one ``DATA`` per ``seconds`` window.'
---

Rate-limit: emit at most one ``DATA`` per ``seconds`` window.

## Signature

```python
def throttle(seconds: float, *, leading: bool = True, trailing: bool = False) -> PipeOperator
```

## Documentation

Rate-limit: emit at most one ``DATA`` per ``seconds`` window.

Args:
    seconds: Window length in seconds.
    leading: Emit the first value at the window start (default ``True``).
    trailing: Emit the latest suppressed value when the window closes.

Returns:
    A unary pipe operator ``(Node) -&gt; Node``.

Example:
    ```python
    from graphrefly import state, pipe
    from graphrefly.extra.tier2 import throttle
    src = state(0)
    out = pipe(src, throttle(0.1))
    ```
