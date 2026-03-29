---
title: 'take'
description: 'Emit at most ``n`` wire ``DATA`` values, then ``COMPLETE`` (``RESOLVED`` does not count).'
---

Emit at most ``n`` wire ``DATA`` values, then ``COMPLETE`` (``RESOLVED`` does not count).

## Signature

```python
def take(n: int) -> PipeOperator
```

## Documentation

Emit at most ``n`` wire ``DATA`` values, then ``COMPLETE`` (``RESOLVED`` does not count).

Args:
    n: Maximum ``DATA`` emissions; ``n &lt;= 0`` completes immediately.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import take as grf_take
    &gt;&gt;&gt; n = pipe(state(0), grf_take(3))
