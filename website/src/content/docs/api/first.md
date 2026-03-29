---
title: 'first'
description: 'Emit the first ``DATA`` then ``COMPLETE`` (same as ``take(1)``).'
---

Emit the first ``DATA`` then ``COMPLETE`` (same as ``take(1)``).

## Signature

```python
def first() -> PipeOperator
```

## Documentation

Emit the first ``DATA`` then ``COMPLETE`` (same as ``take(1)``).

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import first as grf_first
    &gt;&gt;&gt; n = pipe(state(42), grf_first())
