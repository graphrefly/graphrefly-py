---
title: 'retry'
description: 'Retry upstream after ``ERROR`` with optional backoff between attempts.'
---

Retry upstream after ``ERROR`` with optional backoff between attempts.

Unsubscribes from the source after each terminal ``ERROR``, waits (possibly
zero), then resubscribes. Successful ``DATA`` resets the attempt counter.
For a useful retry, the source should use ``resubscribable=True`` so a new
subscription can deliver again after a terminal error.

## Signature

```python
def retry(
    source: Node[Any],
    count: int | None = None,
    *,
    backoff: BackoffStrategy | BackoffPreset | None = None,
) -> Node[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `source` | The upstream :class:`~graphrefly.core.node.Node` to retry. |
| `count` | Maximum retry attempts (``None`` = unlimited when *backoff* is set, 0 otherwise). |
| `backoff` | Optional :data:`~graphrefly.extra.backoff.BackoffStrategy` or :data:`~graphrefly.extra.backoff.BackoffPreset` name for delay between retries. |

## Returns

A new :class:`~graphrefly.core.node.Node` wrapping the retry logic.

## Basic Usage

```python
from graphrefly.extra.resilience import retry
from graphrefly.extra import of
n = retry(of(1), count=3)
```
