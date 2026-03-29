---
title: 'concat'
description: 'Play ``first`` to completion, then continue with ``second``.'
---

Play ``first`` to completion, then continue with ``second``.

## Signature

```python
def concat(second: Node[Any]) -> PipeOperator
```

## Documentation

Play ``first`` to completion, then continue with ``second``.

While ``first`` is active, ``DATA`` from ``second`` is buffered and replayed at handoff.

Args:
    second: Segment played after the primary completes.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import concat as grf_cat
    &gt;&gt;&gt; n = pipe(state(1), grf_cat(state(2)))
