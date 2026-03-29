---
title: 'from_cron'
description: 'Fire on each wall-clock minute matching a 5-field cron expression.'
---

Fire on each wall-clock minute matching a 5-field cron expression.

## Signature

```python
def from_cron(expr: str, *, tick_s: float = 60.0) -> Node[Any]
```

## Documentation

Fire on each wall-clock minute matching a 5-field cron expression.

Emits ``time.time_ns()`` (nanosecond timestamp) on each match.
Uses a built-in cron parser (no external dependencies).
