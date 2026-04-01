---
title: 'never'
description: 'Create a source that never emits any messages.'
---

Create a source that never emits any messages.

## Signature

```python
def never() -> Node[Any]
```

## Documentation

Create a source that never emits any messages.

Returns:
    A :class:`~graphrefly.core.node.Node` whose producer is a no-op
    (no ``DATA``, no ``COMPLETE``).

Example:
    ```python
    from graphrefly.extra import never
    n = never()
    # n.get() is None; no DATA will ever arrive
    ```
