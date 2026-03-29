---
title: 'last'
description: 'Buffer ``DATA``; on ``COMPLETE``, emit the final value (or ``default``).'
---

Buffer ``DATA``; on ``COMPLETE``, emit the final value (or ``default``).

## Signature

```python
def last(*, default: Any = _LAST_NO_DEFAULT) -> PipeOperator
```

## Documentation

Buffer ``DATA``; on ``COMPLETE``, emit the final value (or ``default``).

If no ``default`` is given and the source completes without emitting, only ``COMPLETE``
is forwarded (no ``DATA``).

Args:
    default: Optional value emitted when the source completes empty.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import last as grf_last
    &gt;&gt;&gt; n = pipe(state(1), grf_last(default=0))
