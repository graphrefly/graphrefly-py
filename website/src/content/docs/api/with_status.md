---
title: 'with_status'
description: 'Mirror *src* with ``status`` and ``error`` companion state nodes.'
---

Mirror *src* with ``status`` and ``error`` companion state nodes.

## Signature

```python
def with_status(
    src: Node[Any],
    *,
    initial_status: StatusValue = "pending",
) -> WithStatusBundle
```

## Documentation

Mirror *src* with ``status`` and ``error`` companion state nodes.

``status`` moves ``pending`` → ``active`` on ``DATA``, ``completed`` on ``COMPLETE``, and
``errored`` on ``ERROR`` (``error`` holds the exception). After ``errored``, the next
``DATA`` clears ``error`` and sets ``active`` inside :func:`~graphrefly.core.protocol.batch`.

Companions are wired into ``node.meta`` so they appear in ``describe()``.
