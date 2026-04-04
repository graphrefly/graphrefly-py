---
title: 'decorrelated_jitter'
description: 'Decorrelated jitter (AWS-recommended): ``random(base_ns, min(max, prev * 3))``.'
---

Decorrelated jitter (AWS-recommended): ``random(base_ns, min(max, prev * 3))``.

Stateless — uses ``prev_delay`` (passed by the consumer) instead of closure state.
Safe to share across concurrent retry sequences.

## Signature

```python
def decorrelated_jitter(
    base_ns: int = 100_000_000,
    max_delay_ns: int = 30_000_000_000,
) -> BackoffStrategy
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `base_ns` | Floor of the random range (nanoseconds, default ``100_000_000`` = 100 ms). |
| `max_delay_ns` | Ceiling cap (nanoseconds, default ``30_000_000_000`` = 30 s). |
