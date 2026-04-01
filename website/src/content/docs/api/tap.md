---
title: 'tap'
description: 'Invoke side effects for each emission; value passes through unchanged.'
---

Invoke side effects for each emission; value passes through unchanged.

## Signature

```python
def tap(fn_or_observer: Callable[[Any], None] | dict[str, Callable[..., None]]) -> PipeOperator
```

## Documentation

Invoke side effects for each emission; value passes through unchanged.

When ``fn_or_observer`` is a callable, it is invoked for each ``DATA`` value (classic mode).

When ``fn_or_observer`` is a dict, it may contain keys ``data``, ``error``, and ``complete``,
each a callable invoked for the corresponding message type:

- ``data(value)`` — called on each ``DATA``
- ``error(err)`` — called on ``ERROR``
- ``complete()`` — called on ``COMPLETE``

Args:
    fn_or_observer: A callable ``(value) -&gt; None`` or an observer dict.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, state
    &gt;&gt;&gt; from graphrefly.extra import tap as grf_tap
    &gt;&gt;&gt; n = pipe(state(1), grf_tap(lambda x: None))
    &gt;&gt;&gt; n2 = pipe(state(1), grf_tap({"data": lambda x: None, "complete": lambda: None}))
