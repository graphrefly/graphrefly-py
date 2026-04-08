---
title: 'reactive_list'
description: 'Creates a reactive list backed by an immutable tuple snapshot.'
---

Creates a reactive list backed by an immutable tuple snapshot.

## Signature

```python
def reactive_list(
    initial: Sequence[Any] | None = None,
    *,
    name: str | None = None,
) -> ReactiveListBundle
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `initial` | Optional initial sequence. |
| `name` | Optional registry name for ``describe()`` / debugging. |

## Returns

A :class:`ReactiveListBundle` with ``append`` / ``insert`` / ``pop`` / ``clear``.
