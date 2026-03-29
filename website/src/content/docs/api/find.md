---
title: 'find'
description: 'Emit the first value satisfying ``predicate``, then ``COMPLETE``.'
---

Emit the first value satisfying ``predicate``, then ``COMPLETE``.

## Signature

```python
def find(predicate: Callable[[Any], bool]) -> PipeOperator
```

## Documentation

Emit the first value satisfying ``predicate``, then ``COMPLETE``.

Args:
    predicate: Match test.

Returns:
    A unary callable ``(Node) -&gt; Node`` composed from ``filter`` and ``take(1)``.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import find as grf_find
    &gt;&gt;&gt; n = pipe(state(1), grf_find(lambda x: x &gt; 0))
