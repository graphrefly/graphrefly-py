---
title: 'from_fs_watch'
description: 'Watch filesystem changes and emit debounced events.'
---

Watch filesystem changes and emit debounced events.

## Signature

```python
def from_fs_watch(
    paths: str | list[str],
    *,
    recursive: bool = True,
    debounce: float = 0.1,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    **kwargs: Any,
) -> Node[Any]
```

## Documentation

Watch filesystem changes and emit debounced events.

This source intentionally uses event-driven OS watchers only (no polling fallback).
