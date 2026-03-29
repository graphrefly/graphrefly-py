---
title: 'rate_limiter'
description: 'Sliding-window limit: at most *max_events* ``DATA`` emissions per *window_seconds*.'
---

Sliding-window limit: at most *max_events* ``DATA`` emissions per *window_seconds*.

## Signature

```python
def rate_limiter(max_events: int, window_seconds: float) -> PipeOperator
```

## Documentation

Sliding-window limit: at most *max_events* ``DATA`` emissions per *window_seconds*.

Values that exceed the window budget are queued (FIFO) and emitted as slots free.
``DIRTY`` / ``RESOLVED`` still track the primary when not blocked.
