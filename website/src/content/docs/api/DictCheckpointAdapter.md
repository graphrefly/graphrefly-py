---
title: 'DictCheckpointAdapter'
description: 'Store a checkpoint under a fixed key inside a caller-owned ``dict``.'
---

Store a checkpoint under a fixed key inside a caller-owned ``dict``.

Useful for tests or environments where you already manage a shared dict.

## Signature

```python
class DictCheckpointAdapter
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `storage` | The dict to store the checkpoint in. |
| `key` | Key under which the snapshot is stored (default ``"graphrefly_checkpoint"``). |

## Basic Usage

```python
from graphrefly.extra.checkpoint import DictCheckpointAdapter
store = {}
adapter = DictCheckpointAdapter(store)
adapter.save({"version": 1, "nodes": {}, "edges": [], "subgraphs": [], "name": "g"})
assert "graphrefly_checkpoint" in store
```
