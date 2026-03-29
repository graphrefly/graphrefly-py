---
title: 'retry'
description: 'Retry upstream after ``ERROR`` with optional backoff (threading timer between attempts).'
---

Retry upstream after ``ERROR`` with optional backoff (threading timer between attempts).

## Signature

```python
def retry(
    count: int | None = None,
    *,
    backoff: BackoffStrategy | BackoffPreset | None = None,
) -> PipeOperator
```

## Documentation

Retry upstream after ``ERROR`` with optional backoff (threading timer between attempts).

Unsubscribes from the source after each terminal ``ERROR``, waits (possibly zero), then
resubscribes. Successful ``DATA`` resets the attempt counter.

For a useful retry after upstream ``ERROR``, the source should be
:func:`~graphrefly.core.node.node` with ``resubscribable=True`` so a new subscription can
deliver again after a terminal error.
