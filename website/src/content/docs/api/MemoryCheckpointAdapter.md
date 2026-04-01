---
title: 'MemoryCheckpointAdapter'
description: 'In-memory checkpoint adapter (process-local; useful for tests and embedding).'
---

In-memory checkpoint adapter (process-local; useful for tests and embedding).

## Signature

```python
class MemoryCheckpointAdapter
```

## Documentation

In-memory checkpoint adapter (process-local; useful for tests and embedding).

Stores a deep-copy of the snapshot so mutations to the saved dict do not
affect the stored state.

Example:
    ```python
    from graphrefly import Graph, state
    from graphrefly.extra.checkpoint import MemoryCheckpointAdapter, save_graph_checkpoint
    g = Graph("g"); g.add("x", state(1))
    adapter = MemoryCheckpointAdapter()
    save_graph_checkpoint(g, adapter)
    assert adapter.load()["nodes"]["x"]["value"] == 1
    ```
