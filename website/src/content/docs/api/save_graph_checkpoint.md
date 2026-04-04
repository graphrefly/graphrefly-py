---
title: 'save_graph_checkpoint'
description: 'Persist a :meth:`~graphrefly.graph.Graph.snapshot` via a :class:`CheckpointAdapter`.'
---

Persist a :meth:`~graphrefly.graph.Graph.snapshot` via a :class:`CheckpointAdapter`.

## Signature

```python
def save_graph_checkpoint(graph: Graph, adapter: CheckpointAdapter) -> None
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `graph` | The :class:`~graphrefly.graph.Graph` to snapshot. |
| `adapter` | Any :class:`CheckpointAdapter` (memory, file, SQLite, etc.). |

## Basic Usage

```python
from graphrefly import Graph, state
from graphrefly.extra.checkpoint import MemoryCheckpointAdapter, save_graph_checkpoint
g = Graph("g"); g.add("x", state(5))
adapter = MemoryCheckpointAdapter()
save_graph_checkpoint(g, adapter)
```
