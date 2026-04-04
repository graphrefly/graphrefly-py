---
title: 'PubSubHub'
description: 'Lazy per-topic source node registry for pub/sub messaging.'
---

Lazy per-topic source node registry for pub/sub messaging.

Topics are created on first access as independent manual source nodes.
Use :meth:`publish` to push values via the two-phase ``DIRTY`` then ``DATA``
protocol. Thread-safe.

## Signature

```python
class PubSubHub
```

## Basic Usage

```python
from graphrefly.extra import pubsub
from graphrefly.extra.sources import for_each
hub = pubsub()
log = []
unsub = for_each(hub.topic("events"), log.append)
hub.publish("events", "hello")
unsub()
assert log == ["hello"]
```
