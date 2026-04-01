---
title: 'replay'
description: 'Multicast with late-subscriber replay of the last *buffer_size* ``DATA`` payloads.'
---

Multicast with late-subscriber replay of the last *buffer_size* ``DATA`` payloads.

## Signature

```python
def replay[T](source: Node[T], buffer_size: int = 1) -> Node[T]
```

## Documentation

Multicast with late-subscriber replay of the last *buffer_size* ``DATA`` payloads.

Args:
    source: The upstream node to multicast.
    buffer_size: Number of ``DATA`` payloads to buffer for late joiners (&gt;= 1).

Returns:
    A :class:`~graphrefly.core.node.Node` that replays buffered values to
    each new subscriber before connecting the live stream.

Example:
    ```python
    from graphrefly import state
    from graphrefly.extra.sources import replay, for_each
    x = state(0)
    r = replay(x, buffer_size=2)
    x.down([("DATA", 1)])
    x.down([("DATA", 2)])
    received = []
    unsub = for_each(r, received.append)
    # received includes replayed values 1 and 2
    unsub()
    ```
