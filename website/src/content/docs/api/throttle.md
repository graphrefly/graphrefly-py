---
title: 'throttle'
description: 'Rate-limit: at most one emit per ``seconds`` window.'
---

Rate-limit: at most one emit per ``seconds`` window.

## Signature

```python
def throttle(
    seconds: float, *, leading: bool = True, trailing: bool = False
) -> PipeOperator
```

## Documentation

Rate-limit: at most one emit per ``seconds`` window.

Args:
    seconds: Window length in seconds.
    leading: Whether to emit the first value at the start of each window (default ``True``).
    trailing: Whether to emit the latest suppressed value when the window closes.
