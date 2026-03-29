---
title: 'batch'
description: 'Defer phase-2 messages (DATA, RESOLVED) until the outermost batch exits.'
---

Defer phase-2 messages (DATA, RESOLVED) until the outermost batch exits.

## Signature

```python
def batch() -> Generator[None]
```

## Documentation

Defer phase-2 messages (DATA, RESOLVED) until the outermost batch exits.

DIRTY and non-phase-2 types propagate immediately. Nested batches share one
defer queue; flush runs only when the outermost context exits.

If the outermost context exits with an exception, deferred phase-2 work for
that frame is discarded — phase-2 is not flushed after an error. While the
drain loop is running (``flush_in_progress``), a nested ``batch()`` that
throws must **not** clear the global queue (cross-language decision A4;
matches graphrefly-ts ``batch``).

Each thread has isolated batch state (GRAPHREFLY-SPEC § 4.2).

While deferred work is running, :func:`is_batching` remains true so nested
:func:`emit_with_batch` calls (``defer_when="batching"``) still defer
DATA/RESOLVED until the queue drains.
