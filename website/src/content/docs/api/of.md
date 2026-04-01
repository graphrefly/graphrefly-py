---
title: 'of'
description: 'Emit each argument as ``DATA`` in order, then ``COMPLETE`` on subscribe.'
---

Emit each argument as ``DATA`` in order, then ``COMPLETE`` on subscribe.

## Signature

```python
def of(*values: Any) -> Node[Any]
```

## Documentation

Emit each argument as ``DATA`` in order, then ``COMPLETE`` on subscribe.

Args:
    *values: Values to emit sequentially as ``DATA`` messages.

Returns:
    A cold :class:`~graphrefly.core.node.Node` that completes after emitting all values.

Example:
    ```python
    from graphrefly.extra import of
    from graphrefly.extra.sources import first_value_from
    n = of(1, 2, 3)
    assert first_value_from(n) == 1
    ```
