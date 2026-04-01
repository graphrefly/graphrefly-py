---
title: 'reduce'
description: 'Reduce to one value emitted when the source completes.'
---

Reduce to one value emitted when the source completes.

## Signature

```python
def reduce(  # noqa: A001 — roadmap API name
    reducer: Callable[[Any, Any], Any],
    seed: Any,
) -> PipeOperator
```

## Documentation

Reduce to one value emitted when the source completes.

Unlike RxJS, seed is always required. If the source completes without emitting DATA,
the seed value is emitted (RxJS would throw without a seed).

On an empty completion (no prior ``DATA``), emits ``seed``.

Args:
    reducer: Accumulator update (return value is not used until completion).
    seed: Value used when the source completes with no prior ``DATA``.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import reduce as grf_reduce
    &gt;&gt;&gt; n = pipe(state(1), grf_reduce(lambda a, x: a + x, 0))
