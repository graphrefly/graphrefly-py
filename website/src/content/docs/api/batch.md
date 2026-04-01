---
title: 'batch'
description: 'Defer phase-2 messages (DATA, RESOLVED) until the outermost batch exits.'
---

Defer phase-2 messages (DATA, RESOLVED) until the outermost batch exits.

## Signature

```python
def batch() -> Generator[None]
```

## Documentation

Defer phase-2 messages (DATA, RESOLVED) until the outermost batch exits.

``DIRTY`` and non-phase-2 types propagate immediately. Nested batches share
one defer queue; flush runs only when the outermost context exits. Each
thread has isolated batch state (GRAPHREFLY-SPEC §4.2).

If the outermost context exits with an exception, deferred phase-2 work is
discarded. While the drain loop is running (``flush_in_progress``), nested
:func:`emit_with_batch` calls with ``defer_when="batching"`` still defer
``DATA``/``RESOLVED`` until the queue drains.

Returns:
    A context manager that yields ``None``; has no return value.

Example:
    ```python
    from graphrefly import state, batch
    x = state(0)
    y = state(0)
    with batch():
        x.down([("DATA", 1)])
        y.down([("DATA", 2)])
    # Downstream sees both updates atomically
    ```
