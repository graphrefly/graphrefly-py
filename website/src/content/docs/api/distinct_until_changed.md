---
title: 'distinct_until_changed'
description: 'Suppress consecutive duplicates using ``equals`` (default: ``operator.is_``).'
---

Suppress consecutive duplicates using ``equals`` (default: ``operator.is_``).

## Signature

```python
def distinct_until_changed(
    equals: Callable[[Any, Any], bool] | None = None,
) -> PipeOperator
```

## Documentation

Suppress consecutive duplicates using ``equals`` (default: ``operator.is_``).

Args:
    equals: Optional binary equality for adjacent values.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import distinct_until_changed as grf_duc
    &gt;&gt;&gt; n = pipe(state(1), grf_duc())
