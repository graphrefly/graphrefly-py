---
title: 'pausable'
description: 'Buffer ``DIRTY``/``DATA``/``RESOLVED`` while ``PAUSE`` is in effect; flush on ``RESUME``.'
---

Buffer ``DIRTY``/``DATA``/``RESOLVED`` while ``PAUSE`` is in effect; flush on ``RESUME``.

## Signature

```python
def pausable() -> PipeOperator
```

## Documentation

Buffer ``DIRTY``/``DATA``/``RESOLVED`` while ``PAUSE`` is in effect; flush on ``RESUME``.

Protocol-level pause/resume using ``PAUSE``/``RESUME`` message types. Matches
TypeScript ``pausable`` semantics.

Returns:
    A unary pipe operator ``(Node) -&gt; Node``.

Example:
    ```python
    from graphrefly import state, pipe
    from graphrefly.extra.tier2 import pausable
    from graphrefly.core.protocol import MessageType
    src = state(0)
    out = pipe(src, pausable())
    out.down([(MessageType.PAUSE,)])
    out.down([(MessageType.RESUME,)])
    ```
