---
title: 'flat_map'
description: 'Map each outer value to an inner node; subscribe to every inner concurrently (merge).'
---

Map each outer value to an inner node; subscribe to every inner concurrently (merge).

## Signature

```python
def flat_map(fn: Callable[[Any], Node[Any]], *, initial: Any = _UNSET) -> PipeOperator
```

## Documentation

Map each outer value to an inner node; subscribe to every inner concurrently (merge).

Completes when the outer has completed and every inner subscription has ended.

Args:
    fn: ``outer_value -&gt; Node``.
    initial: Optional initial ``get()`` value.

Returns:
    A unary pipe operator.
