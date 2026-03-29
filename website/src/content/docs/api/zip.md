---
title: 'zip'
description: 'Zip one ``DATA`` from each source per cycle into a tuple.'
---

Zip one ``DATA`` from each source per cycle into a tuple.

## Signature

```python
def zip(  # noqa: A001
    *sources: Node[Any],
    max_buffer: int = 0,
) -> Node[Any]
```

## Documentation

Zip one ``DATA`` from each source per cycle into a tuple.

Args:
    *sources: Upstreams to zip.
    max_buffer: When ``&gt; 0``, drop oldest queued values per source beyond this depth.

Returns:
    A :class:`~graphrefly.core.node.Node` emitting tuples.

Examples:
    &gt;&gt;&gt; from graphrefly.extra import zip as grf_zip
    &gt;&gt;&gt; from graphrefly import state
    &gt;&gt;&gt; n = grf_zip(state(1), state(2))
