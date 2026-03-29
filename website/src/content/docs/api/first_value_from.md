---
title: 'first_value_from'
description: 'The synchronous bridge: block until the first ``DATA`` or terminal ``ERROR``.'
---

The synchronous bridge: block until the first ``DATA`` or terminal ``ERROR``.

## Signature

```python
def first_value_from(
    source: Node[Any],
    *,
    timeout: float | None = None,
) -> Any
```

## Documentation

The synchronous bridge: block until the first ``DATA`` or terminal ``ERROR``.

On ``COMPLETE`` without prior ``DATA``, raises :class:`StopIteration`. With *timeout*,
raises :class:`TimeoutError` if no terminal message arrives in time.
