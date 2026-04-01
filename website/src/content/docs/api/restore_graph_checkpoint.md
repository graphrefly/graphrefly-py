---
title: 'restore_graph_checkpoint'
description: 'Load a snapshot from *adapter* and apply it to *graph*.'
---

Load a snapshot from *adapter* and apply it to *graph*.

## Signature

```python
def restore_graph_checkpoint(graph: Graph, adapter: CheckpointAdapter) -> bool
```

## Documentation

Load a snapshot from *adapter* and apply it to *graph*.

Uses :meth:`~graphrefly.graph.Graph.restore` for application.

Args:
    graph: The target :class:`~graphrefly.graph.Graph`.
    adapter: Any :class:`CheckpointAdapter` to load from.

Returns:
    ``True`` if snapshot data existed and was applied; ``False`` if the adapter
    had no saved data.

Example:
    ```python
    from graphrefly import Graph, state
    from graphrefly.extra.checkpoint import (
        MemoryCheckpointAdapter,
        restore_graph_checkpoint,
        save_graph_checkpoint,
    )
    g = Graph("g"); x = state(0); g.add("x", x)
    adapter = MemoryCheckpointAdapter()
    save_graph_checkpoint(g, adapter)
    x.down([("DATA", 99)])
    restored = restore_graph_checkpoint(g, adapter)
    assert restored and g.get("x") == 0
    ```
