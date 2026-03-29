---
title: 'throttle'
description: 'Leading-edge rate limit: first ``DATA`` in each window emits immediately; others drop.'
---

Leading-edge rate limit: first ``DATA`` in each window emits immediately; others drop.

## Signature

```python
def throttle(seconds: float, *, trailing: bool = False) -> PipeOperator
```

## Documentation

Leading-edge rate limit: first ``DATA`` in each window emits immediately; others drop.

When ``trailing=True``, one additional emit is scheduled when the window ends if the
source emitted newer values that were suppressed during the window.

Args:
    seconds: Window length in seconds.
    trailing: Whether to emit the latest suppressed value when the window closes.
