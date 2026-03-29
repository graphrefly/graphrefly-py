---
title: 'window'
description: 'Split source ``DATA`` into sub-node windows; new window on each notifier ``DATA``.'
---

Split source ``DATA`` into sub-node windows; new window on each notifier ``DATA``.

## Signature

```python
def window(notifier: Node[Any]) -> PipeOperator
```

## Documentation

Split source ``DATA`` into sub-node windows; new window on each notifier ``DATA``.

Each emitted value is a :class:`~graphrefly.core.node.Node` that receives
the values belonging to that window.
