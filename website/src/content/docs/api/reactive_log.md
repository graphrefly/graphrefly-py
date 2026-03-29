---
title: 'reactive_log'
description: 'Creates an append-only reactive log (tuple snapshot).'
---

Creates an append-only reactive log (tuple snapshot).

## Signature

```python
def reactive_log(
    initial: Sequence[Any] | None = None,
    *,
    name: str | None = None,
) -> ReactiveLogBundle
```

## Documentation

Creates an append-only reactive log (tuple snapshot).

Args:
    initial: Optional seed sequence; copied to a tuple.
    name: Optional registry name for ``describe()`` / debugging.

Returns:
    A :class:`ReactiveLogBundle` with ``append`` / ``clear`` and
    :meth:`~ReactiveLogBundle.tail`.

Examples:
    &gt;&gt;&gt; from graphrefly.extra import reactive_log
    &gt;&gt;&gt; lg = reactive_log([1, 2])
    &gt;&gt;&gt; lg.append(3)
    &gt;&gt;&gt; lg.entries.get().value
    (1, 2, 3)
