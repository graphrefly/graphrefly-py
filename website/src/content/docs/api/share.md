---
title: 'share'
description: 'Share one upstream subscription across all downstream sinks (ref-counted).'
---

Share one upstream subscription across all downstream sinks (ref-counted).

## Signature

```python
def share[T](source: Node[T]) -> Node[T]
```

## Documentation

Share one upstream subscription across all downstream sinks (ref-counted).

Args:
    source: The upstream node to multicast.

Returns:
    A new :class:`~graphrefly.core.node.Node` that connects to *source* once
    and ref-counts downstream subscriptions.

Example:
    ```python
    from graphrefly import state
    from graphrefly.extra.sources import share, for_each
    x = state(0)
    s = share(x)
    log = []
    unsub = for_each(s, log.append)
    x.down([("DATA", 1)])
    unsub()
    ```
