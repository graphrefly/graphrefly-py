---
title: 'PubSubHub'
description: 'Lazy per-topic :func:`~graphrefly.core.sugar.state` nodes (created on first access).'
---

Lazy per-topic :func:`~graphrefly.core.sugar.state` nodes (created on first access).

## Signature

```python
class PubSubHub
```

## Documentation

Lazy per-topic :func:`~graphrefly.core.sugar.state` nodes (created on first access).

Thread-safe topic registry. Each topic is an independent manual source node; use
:meth:`publish` to push values with two-phase ``DIRTY`` then ``DATA``.
