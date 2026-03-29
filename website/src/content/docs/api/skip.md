---
title: 'skip'
description: 'Drop the first ``n`` wire ``DATA`` payloads (``RESOLVED`` does not advance the counter).'
---

Drop the first ``n`` wire ``DATA`` payloads (``RESOLVED`` does not advance the counter).

## Signature

```python
def skip(n: int) -> PipeOperator
```

## Documentation

Drop the first ``n`` wire ``DATA`` payloads (``RESOLVED`` does not advance the counter).

Args:
    n: Number of ``DATA`` values to suppress before forwarding.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import skip as grf_skip
    &gt;&gt;&gt; n = pipe(state(0), grf_skip(2))
