---
title: 'element_at'
description: 'Emit the value at zero-based ``DATA`` index ``index``, then ``COMPLETE``.'
---

Emit the value at zero-based ``DATA`` index ``index``, then ``COMPLETE``.

## Signature

```python
def element_at(index: int) -> PipeOperator
```

## Documentation

Emit the value at zero-based ``DATA`` index ``index``, then ``COMPLETE``.

Args:
    index: Number of prior ``DATA`` emissions to skip.

Returns:
    A unary callable ``(Node) -&gt; Node`` composed from ``skip`` and ``take(1)``.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import element_at as grf_at
    &gt;&gt;&gt; n = pipe(state(0), grf_at(2))
