---
title: 'concat_map'
description: 'Map outer values to inner nodes; run inners strictly one after another.'
---

Map outer values to inner nodes; run inners strictly one after another.

## Signature

```python
def concat_map(
    fn: Callable[[Any], Node[Any]],
    *,
    initial: Any = _UNSET,
    max_buffer: int = 0,
) -> PipeOperator
```

## Documentation

Map outer values to inner nodes; run inners strictly one after another.

While an inner is active, outer ``DATA`` values are queued. ``max_buffer &gt; 0`` drops the
oldest queued value when the queue would exceed that length.

Args:
    fn: ``outer_value -&gt; Node``.
    initial: Optional initial ``get()`` value.
    max_buffer: Maximum queued outer keys (``0`` = unlimited).

Returns:
    A unary pipe operator.
