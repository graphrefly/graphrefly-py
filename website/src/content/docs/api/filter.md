---
title: 'filter'
description: 'Forward values where ``predicate`` is true; otherwise emit ``RESOLVED`` (no ``DATA``).'
---

Forward values where ``predicate`` is true; otherwise emit ``RESOLVED`` (no ``DATA``).

## Signature

```python
def filter(
    predicate: Callable[[Any], bool],
) -> PipeOperator
```

## Documentation

Forward values where ``predicate`` is true; otherwise emit ``RESOLVED`` (no ``DATA``).

Pure predicate gate — no implicit dedup (use ``distinct_until_changed`` for that).

Args:
    predicate: Inclusion test for each value.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import filter as grf_filter
    &gt;&gt;&gt; n = pipe(state(1), grf_filter(lambda x: x &gt; 0))
