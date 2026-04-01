---
title: 'throw_error'
description: 'Emit a single ``ERROR`` message when the first sink subscribes.'
---

Emit a single ``ERROR`` message when the first sink subscribes.

## Signature

```python
def throw_error(error: BaseException | Any) -> Node[Any]
```

## Documentation

Emit a single ``ERROR`` message when the first sink subscribes.

Args:
    error: The exception or value to send as the ``ERROR`` payload.

Returns:
    A :class:`~graphrefly.core.node.Node` that immediately errors on subscribe.

Example:
    ```python
    from graphrefly.extra import throw_error
    n = throw_error(ValueError("bad"))
    try:
        from graphrefly.extra.sources import first_value_from
        first_value_from(n)
    except ValueError:
        pass
    ```
