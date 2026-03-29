---
title: 'pairwise'
description: 'Emit ``(previous, current)`` pairs; the first upstream value yields ``RESOLVED`` only.'
---

Emit ``(previous, current)`` pairs; the first upstream value yields ``RESOLVED`` only.

## Signature

```python
def pairwise() -> PipeOperator
```

## Documentation

Emit ``(previous, current)`` pairs; the first upstream value yields ``RESOLVED`` only.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import pairwise as grf_pw
    &gt;&gt;&gt; n = pipe(state(0), grf_pw())
