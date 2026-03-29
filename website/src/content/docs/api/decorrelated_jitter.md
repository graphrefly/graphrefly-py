---
title: 'decorrelated_jitter'
description: 'Decorrelated jitter (AWS-recommended): ``random(base, min(max, prev * 3))``.'
---

Decorrelated jitter (AWS-recommended): ``random(base, min(max, prev * 3))``.

## Signature

```python
def decorrelated_jitter(
    base: float = 0.1,
    max_delay: float = 30.0,
) -> BackoffStrategy
```

## Documentation

Decorrelated jitter (AWS-recommended): ``random(base, min(max, prev * 3))``.

Stateless — uses ``prev_delay`` (passed by the consumer) instead of closure state.
Safe to share across concurrent retry sequences.

Args:
    base: Floor of the random range (seconds, default ``0.1``).
    max_delay: Ceiling cap (seconds, default ``30.0``).
