---
title: 'with_latest_from'
description: 'When the primary source settles, emit ``(primary, latest_secondary)``.'
---

When the primary source settles, emit ``(primary, latest_secondary)``.

## Signature

```python
def with_latest_from(other: Node[Any]) -> PipeOperator
```

## Documentation

When the primary source settles, emit ``(primary, latest_secondary)``.

Updates from ``other`` alone refresh the cached secondary value but do not emit.

Args:
    other: Secondary node whose latest value is paired on primary emissions.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import with_latest_from as grf_wlf
    &gt;&gt;&gt; n = pipe(state(1), grf_wlf(state("x")))
