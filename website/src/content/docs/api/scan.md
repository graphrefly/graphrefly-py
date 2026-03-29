---
title: 'scan'
description: 'Fold upstream values with ``reducer(acc, value) -> acc``; emit accumulator after each step.'
---

Fold upstream values with ``reducer(acc, value) -&gt; acc``; emit accumulator after each step.

## Signature

```python
def scan(
    reducer: Callable[[Any, Any], Any],
    seed: Any,
    *,
    equals: Callable[[Any, Any], bool] | None = None,
) -> PipeOperator
```

## Documentation

Fold upstream values with ``reducer(acc, value) -&gt; acc``; emit accumulator after each step.

Args:
    reducer: Accumulator update.
    seed: Initial accumulator (also used for ``initial`` on the inner node).
    equals: Optional equality for consecutive emissions (default ``operator.is_``).

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import scan as grf_scan
    &gt;&gt;&gt; n = pipe(state(1), grf_scan(lambda a, x: a + x, 0))
