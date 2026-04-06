---
title: 'DictCheckpointAdapter'
description: 'Store checkpoints by key inside a caller-owned ``dict``.'
---

Store checkpoints by key inside a caller-owned ``dict``.

## Signature

```python
class DictCheckpointAdapter
```

## Basic Usage

```python
from graphrefly.extra.checkpoint import DictCheckpointAdapter
store = {}
adapter = DictCheckpointAdapter(store)
data = {"version": 1, "nodes": {}, "edges": [], "subgraphs": [], "name": "g"}
adapter.save("mykey", data)
assert "mykey" in store
```
