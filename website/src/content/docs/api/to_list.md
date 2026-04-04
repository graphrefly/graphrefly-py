---
title: 'to_list'
description: 'Block until ``COMPLETE`` or ``ERROR``, collecting all ``DATA`` payloads in order.'
---

Block until ``COMPLETE`` or ``ERROR``, collecting all ``DATA`` payloads in order.

## Signature

```python
def to_list(
    source: Node[Any],
    *,
    timeout: float | None = None,
) -> list[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `source` | The node to collect from. |
| `timeout` | Optional timeout in seconds; raises :exc:`TimeoutError` if ``COMPLETE`` does not arrive in time. |

## Returns

A list of ``DATA`` payloads in emission order.

## Basic Usage

```python
from graphrefly.extra import of
from graphrefly.extra.sources import to_list
assert to_list(of(1, 2, 3)) == [1, 2, 3]
```
