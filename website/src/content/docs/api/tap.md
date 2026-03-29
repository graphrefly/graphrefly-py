---
title: 'tap'
description: 'Invoke ``side_effect(value)`` for each emission; value passes through unchanged.'
---

Invoke ``side_effect(value)`` for each emission; value passes through unchanged.

## Signature

```python
def tap(side_effect: Callable[[Any], None]) -> PipeOperator
```

## Documentation

Invoke ``side_effect(value)`` for each emission; value passes through unchanged.

Args:
    side_effect: Called for side effects only.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import tap as grf_tap
    &gt;&gt;&gt; n = pipe(state(1), grf_tap(lambda x: None))
