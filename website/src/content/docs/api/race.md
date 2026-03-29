---
title: 'race'
description: 'First source to emit ``DATA`` wins; subsequent traffic follows only that source.'
---

First source to emit ``DATA`` wins; subsequent traffic follows only that source.

## Signature

```python
def race(*sources: Node[Any]) -> Node[Any]
```

## Documentation

First source to emit ``DATA`` wins; subsequent traffic follows only that source.

Args:
    *sources: Contestants (empty → immediate ``COMPLETE``; one node is returned as-is).

Returns:
    A :class:`~graphrefly.core.node.Node`.

Examples:
    &gt;&gt;&gt; from graphrefly.extra import race
    &gt;&gt;&gt; from graphrefly import state
    &gt;&gt;&gt; n = race(state(1), state(2))
