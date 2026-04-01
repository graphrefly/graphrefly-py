---
title: 'from_iter'
description: 'Drain a synchronous iterable on subscribe, emitting one ``DATA`` per item then ``COMPLETE``.'
---

Drain a synchronous iterable on subscribe, emitting one ``DATA`` per item then ``COMPLETE``.

## Signature

```python
def from_iter(iterable: Iterable[Any]) -> Node[Any]
```

## Documentation

Drain a synchronous iterable on subscribe, emitting one ``DATA`` per item then ``COMPLETE``.

If iteration raises an exception, the producer emits ``ERROR`` and stops.

Args:
    iterable: Any synchronous iterable (list, generator, etc.).

Returns:
    A cold :class:`~graphrefly.core.node.Node` that completes after the iterable is drained.

Example:
    ```python
    from graphrefly.extra import from_iter
    from graphrefly.extra.sources import to_list
    assert to_list(from_iter([1, 2, 3])) == [1, 2, 3]
    ```
