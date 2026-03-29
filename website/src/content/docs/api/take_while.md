---
title: 'take_while'
description: 'Emit while ``predicate`` holds; on first false, ``COMPLETE``.'
---

Emit while ``predicate`` holds; on first false, ``COMPLETE``.

## Signature

```python
def take_while(predicate: Callable[[Any], bool]) -> PipeOperator
```

## Documentation

Emit while ``predicate`` holds; on first false, ``COMPLETE``.

Predicate exceptions propagate via node-level error handling (spec §2.4).

Args:
    predicate: Continuation test for each value.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import take_while as grf_tw
    &gt;&gt;&gt; n = pipe(state(1), grf_tw(lambda x: x &lt; 10))
