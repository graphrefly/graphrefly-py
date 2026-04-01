---
title: 'checkpoint_node_value'
description: "Build a minimal versioned JSON payload for a single node's last cached value."
---

Build a minimal versioned JSON payload for a single node's last cached value.

## Signature

```python
def checkpoint_node_value(node: Node[Any]) -> dict[str, Any]
```

## Documentation

Build a minimal versioned JSON payload for a single node's last cached value.

Useful for custom adapters that persist individual nodes rather than whole
graph snapshots. Emits a warning when the value is not JSON-serializable.

Args:
    node: Any :class:`~graphrefly.core.node.Node` whose ``get()`` value to capture.

Returns:
    A ``dict`` with ``version`` (``1``) and ``value`` keys.

Example:
    ```python
    from graphrefly import state
    from graphrefly.extra.checkpoint import checkpoint_node_value
    x = state(42)
    payload = checkpoint_node_value(x)
    assert payload == {"version": 1, "value": 42}
    ```
