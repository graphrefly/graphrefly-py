---
title: 'first_value_from'
description: 'Block until the first ``DATA`` value or a terminal ``ERROR`` arrives.'
---

Block until the first ``DATA`` value or a terminal ``ERROR`` arrives.

## Signature

```python
def first_value_from(
    source: Node[Any],
    *,
    timeout: float | None = None,
) -> Any
```

## Documentation

Block until the first ``DATA`` value or a terminal ``ERROR`` arrives.

On ``COMPLETE`` without prior ``DATA``, raises :exc:`StopIteration`. With
*timeout*, raises :exc:`TimeoutError` if no terminal message arrives in time.

Args:
    source: The node to await the first value from.
    timeout: Optional timeout in seconds.

Returns:
    The first ``DATA`` payload received.

Notes:
    Python exposes this as a synchronous blocking call. The TypeScript equivalent
    ``firstValueFrom`` returns a ``Promise``; both provide the same escape-hatch
    semantics with implementation differences due to language concurrency models.

Example:
    ```python
    from graphrefly.extra import of
    from graphrefly.extra.sources import first_value_from
    assert first_value_from(of(42)) == 42
    ```
