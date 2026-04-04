---
title: 'log_slice'
description: 'Derived view of a slice of the log, same semantics as ``tuple[start:stop]`` (stop exclusive).'
---

Derived view of a slice of the log, same semantics as ``tuple[start:stop]`` (stop exclusive).

## Signature

```python
def log_slice(
    log: ReactiveLogBundle,
    start: int,
    stop: int | None = None,
) -> Node[tuple[Any, ...]]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `log` | A :class:`ReactiveLogBundle`. |
| `start` | Start index (must be &gt;= 0). |
| `stop` | End index (exclusive); if ``None``, slice to the end. |

## Returns

A derived node emitting the sliced tuple; stays updated while the log changes.
