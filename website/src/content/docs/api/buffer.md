---
title: 'buffer'
description: 'Collect ``DATA`` values in a buffer; emit the list when ``notifier`` emits ``DATA``.'
---

Collect ``DATA`` values in a buffer; emit the list when ``notifier`` emits ``DATA``.

## Signature

```python
def buffer(notifier: Node[Any]) -> PipeOperator
```

## Documentation

Collect ``DATA`` values in a buffer; emit the list when ``notifier`` emits ``DATA``.

Args:
    notifier: Node whose ``DATA`` flushes the accumulated buffer.

Returns:
    A unary pipe operator ``(Node) -&gt; Node[list]``.

Example:
    ```python
    from graphrefly import state, pipe
    from graphrefly.extra.tier2 import buffer
    src = state(0)
    flush = state(None)
    out = pipe(src, buffer(flush))
    ```
