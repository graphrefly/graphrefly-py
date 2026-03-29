---
title: 'to_list'
description: 'Block until ``COMPLETE`` or ``ERROR``, collecting ``DATA`` payloads in order.'
---

Block until ``COMPLETE`` or ``ERROR``, collecting ``DATA`` payloads in order.

## Signature

```python
def to_list(
    source: Node[Any],
    *,
    timeout: float | None = None,
) -> list[Any]
```

## Documentation

Block until ``COMPLETE`` or ``ERROR``, collecting ``DATA`` payloads in order.

Uses an internal subscribe + :class:`threading.Event`. On ``ERROR``, raises the error
payload if it is a :class:`BaseException`, otherwise ``RuntimeError``.
