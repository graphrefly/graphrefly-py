---
title: 'exhaust_map'
description: 'Like :func:`switch_map`, but ignores new outer ``DATA`` while the current inner is active.'
---

Like :func:`switch_map`, but ignores new outer ``DATA`` while the current inner is active.

## Signature

```python
def exhaust_map(fn: Callable[[Any], Node[Any]], *, initial: Any = _UNSET) -> PipeOperator
```

## Documentation

Like :func:`switch_map`, but ignores new outer ``DATA`` while the current inner is active.

Args:
    fn: ``outer_value -&gt; Node``.
    initial: Optional initial ``get()`` value.

Returns:
    A unary pipe operator.
