---
title: 'with_status'
description: 'Mirror *src* with ``status`` and ``error`` companion state nodes.'
---

Mirror *src* with ``status`` and ``error`` companion state nodes.

``status`` moves ``pending`` → ``active`` on ``DATA``, ``completed`` on
``COMPLETE``, and ``errored`` on ``ERROR`` (``error`` holds the exception).
After ``errored``, the next ``DATA`` clears ``error`` and sets ``active``
inside :func:`~graphrefly.core.protocol.batch`. Both companions are wired
into ``node.meta`` so they appear in ``describe()``.

## Signature

```python
def with_status(
    src: Node[Any],
    *,
    initial_status: StatusValue = "pending",
) -> WithStatusBundle
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `src` | The upstream :class:`~graphrefly.core.node.Node` to track. |
| `initial_status` | Initial value of the ``status`` companion (default ``"pending"``). |

## Returns

A :class:`WithStatusBundle` with ``node``, ``status``, and ``error`` fields.

## Basic Usage

```python
from graphrefly import state
from graphrefly.extra.resilience import with_status
src = state(None)
bundle = with_status(src)
assert bundle.status.get() == "pending"
```
