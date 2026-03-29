---
title: 'reactive_index'
description: 'Creates a dual-key index: unique primary key, rows sorted by ``(secondary, primary)``.'
---

Creates a dual-key index: unique primary key, rows sorted by ``(secondary, primary)``.

## Signature

```python
def reactive_index(*, name: str | None = None) -> ReactiveIndexBundle[Any]
```

## Documentation

Creates a dual-key index: unique primary key, rows sorted by ``(secondary, primary)``.

Args:
    name: Optional registry name for ``describe()`` / debugging.

Returns:
    A :class:`ReactiveIndexBundle` with ``upsert`` / ``delete`` / ``clear`` and
    ``by_primary`` / ``ordered`` derived nodes.
