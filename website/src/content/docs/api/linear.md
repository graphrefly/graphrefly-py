---
title: 'linear'
description: 'Linear delay: ``base + step * attempt`` (``step`` defaults to ``base``).'
---

Linear delay: ``base + step * attempt`` (``step`` defaults to ``base``).

## Signature

```python
def linear(base: float, step: float | None = None) -> BackoffStrategy
```

## Documentation

Linear delay: ``base + step * attempt`` (``step`` defaults to ``base``).
