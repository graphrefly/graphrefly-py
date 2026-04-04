---
title: 'for_each'
description: 'Subscribe to *source* and invoke ``fn(value)`` for each ``DATA`` message.'
---

Subscribe to *source* and invoke ``fn(value)`` for each ``DATA`` message.

## Signature

```python
def for_each(
    source: Node[Any],
    fn: Callable[[Any], None],
    *,
    on_error: Callable[[BaseException], None] | None = None,
) -> Callable[[], None]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `source` | The node to subscribe to. |
| `fn` | Callback invoked with each ``DATA`` payload. |
| `on_error` | Optional callback invoked when an ``ERROR`` is received. If omitted, the error is re-raised from inside the sink. |

## Returns

An unsubscribe callable; call it to detach.

## Basic Usage

```python
from graphrefly import state
from graphrefly.extra.sources import for_each
x = state(0)
log = []
unsub = for_each(x, log.append)
x.down([("DATA", 7)])
unsub()
assert log == [7]
```
