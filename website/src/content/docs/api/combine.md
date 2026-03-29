---
title: 'combine'
description: 'Combine latest values from all sources into a tuple whenever any settles.'
---

Combine latest values from all sources into a tuple whenever any settles.

## Signature

```python
def combine(*sources: Node[Any]) -> Node[Any]
```

## Documentation

Combine latest values from all sources into a tuple whenever any settles.

Args:
    *sources: Upstream nodes (empty → empty tuple node).

Returns:
    A :class:`~graphrefly.core.node.Node` emitting ``tuple`` of dependency values.

Examples:
    &gt;&gt;&gt; from graphrefly.extra import combine
    &gt;&gt;&gt; from graphrefly import state
    &gt;&gt;&gt; n = combine(state(1), state("a"))
