---
title: 'merge'
description: 'Merge ``DATA`` from any dependency; ``COMPLETE`` only after every source completes.'
---

Merge ``DATA`` from any dependency; ``COMPLETE`` only after every source completes.

## Signature

```python
def merge(*sources: Node[Any]) -> Node[Any]
```

## Documentation

Merge ``DATA`` from any dependency; ``COMPLETE`` only after every source completes.

Args:
    *sources: Upstreams to merge (empty → immediate ``COMPLETE`` node).

Returns:
    A :class:`~graphrefly.core.node.Node`.

Examples:
    &gt;&gt;&gt; from graphrefly.extra import merge
    &gt;&gt;&gt; from graphrefly import state
    &gt;&gt;&gt; n = merge(state(1), state(2))
