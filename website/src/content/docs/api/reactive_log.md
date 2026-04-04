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
    max_size: int | None = None,
    name: str | None = None,
) -> ReactiveLogBundle
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `initial` | Optional seed sequence; copied to a tuple. |
| `max_size` | If set, maximum number of entries; oldest entries are trimmed from the head when the buffer exceeds this size (must be &gt;= 1). |
| `name` | Optional registry name for ``describe()`` / debugging. |

## Returns

A :class:`ReactiveLogBundle` with ``append`` / ``append_many`` /
``trim_head`` / ``clear`` and :meth:`~ReactiveLogBundle.tail`.

## Basic Usage

```python
from graphrefly.extra import reactive_log
lg = reactive_log([1, 2])
lg.append(3)
assert lg.entries.get().value == (1, 2, 3)
```
