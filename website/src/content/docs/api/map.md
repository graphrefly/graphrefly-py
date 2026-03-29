---
title: 'map'
description: 'Map each upstream settled value through ``fn``.'
---

Map each upstream settled value through ``fn``.

## Signature

```python
def map(
    fn: Callable[[Any], Any],
    *,
    equals: Callable[[Any, Any], bool] | None = None,
) -> PipeOperator
```

## Documentation

Map each upstream settled value through ``fn``.

Args:
    fn: Transform applied to each dependency value.
    equals: Optional equality for ``RESOLVED`` detection on the inner node.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator` wrapping the upstream node.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import map as grf_map
    &gt;&gt;&gt; n = pipe(state(1), grf_map(lambda x: x * 2))
