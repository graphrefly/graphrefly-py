---
title: 'start_with'
description: 'Emit ``value`` as ``DATA`` first, then forward every value from the source.'
---

Emit ``value`` as ``DATA`` first, then forward every value from the source.

## Signature

```python
def start_with(value: Any) -> PipeOperator
```

## Documentation

Emit ``value`` as ``DATA`` first, then forward every value from the source.

Args:
    value: Prepended emission before upstream values.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import start_with as grf_sw
    &gt;&gt;&gt; n = pipe(state(2), grf_sw(0))
