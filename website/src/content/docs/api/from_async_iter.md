---
title: 'from_async_iter'
description: 'Iterate an async iterable via a :class:`~graphrefly.core.runner.Runner`.'
---

Iterate an async iterable via a :class:`~graphrefly.core.runner.Runner`.

## Signature

```python
def from_async_iter(
    aiterable: AsyncIterable[Any],
    *,
    runner: Any | None = None,
) -> Node[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `aiterable` | The async iterable to drain. |
| `runner` | Optional :class:`~graphrefly.core.runner.Runner`.  When ``None``, uses the thread-local default runner. |

## Returns

A :class:`~graphrefly.core.node.Node` that emits ``DATA`` per item,
then ``COMPLETE``, or ``ERROR`` on failure.
