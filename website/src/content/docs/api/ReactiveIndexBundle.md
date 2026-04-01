---
title: 'ReactiveIndexBundle'
description: 'Dual-key index: unique primary key with rows sorted by ``(secondary, primary)``.'
---

Dual-key index: unique primary key with rows sorted by ``(secondary, primary)``.

## Signature

```python
class ReactiveIndexBundle[K]
```

## Documentation

Dual-key index: unique primary key with rows sorted by ``(secondary, primary)``.

Attributes:
    by_primary: Derived node mapping ``primary -&gt; value``.
    ordered: Derived node with all rows as a sorted tuple.

Example:
    ```python
    from graphrefly.extra import reactive_index
    idx = reactive_index()
    idx.upsert("alice", score=90, value={"name": "Alice"})
    assert "alice" in idx.by_primary.get()
    ```
