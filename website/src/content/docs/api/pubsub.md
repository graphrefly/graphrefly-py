---
title: 'pubsub'
description: 'Create a new :class:`PubSubHub` with an empty topic registry.'
---

Create a new :class:`PubSubHub` with an empty topic registry.

## Signature

```python
def pubsub() -> PubSubHub
```

## Returns

A fresh :class:`PubSubHub` instance.

## Basic Usage

```python
from graphrefly.extra import pubsub
hub = pubsub()
hub.publish("topic", 1)
assert hub.topic("topic").get() == 1
```
