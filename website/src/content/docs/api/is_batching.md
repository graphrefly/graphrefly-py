---
title: 'is_batching'
description: 'Return True while inside ``batch()`` or while deferred phase-2 work is draining.'
---

Return True while inside ``batch()`` or while deferred phase-2 work is draining.

## Signature

```python
def is_batching() -> bool
```

## Documentation

Return True while inside ``batch()`` or while deferred phase-2 work is draining.

Returns:
    ``True`` when batch depth &gt; 0 or the drain loop is active.

Example:
    ```python
    from graphrefly import batch, is_batching
    assert not is_batching()
    with batch():
        assert is_batching()
    ```
