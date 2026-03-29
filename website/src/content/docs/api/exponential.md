---
title: 'exponential'
description: 'Exponential delay capped by ``max_delay`` (optional jitter).'
---

Exponential delay capped by ``max_delay`` (optional jitter).

## Signature

```python
def exponential(
    *,
    base: float = 0.1,
    factor: float = 2.0,
    max_delay: float = 30.0,
    jitter: JitterMode = "none",
) -> BackoffStrategy
```

## Documentation

Exponential delay capped by ``max_delay`` (optional jitter).
