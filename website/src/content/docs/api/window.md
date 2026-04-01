---
title: 'window'
description: 'Split source ``DATA`` into sub-node windows; open a new window on each notifier ``DATA``.'
---

Split source ``DATA`` into sub-node windows; open a new window on each notifier ``DATA``.

## Signature

```python
def window(notifier: Node[Any]) -> PipeOperator
```

## Documentation

Split source ``DATA`` into sub-node windows; open a new window on each notifier ``DATA``.

Each emitted value is a :class:`~graphrefly.core.node.Node` receiving the
``DATA`` values belonging to that window.

Args:
    notifier: Node whose ``DATA`` opens a new window.

Returns:
    A unary pipe operator ``(Node) -&gt; Node[Node]``.

Example:
    ```python
    from graphrefly import state, pipe
    from graphrefly.extra.tier2 import window
    src = state(0)
    tick = state(None)
    out = pipe(src, window(tick))
    ```
