---
title: 'fibonacci'
description: 'Fibonacci-scaled delay: ``1, 2, 3, 5, … × base`` per attempt, capped at ``max_delay``.'
---

Fibonacci-scaled delay: ``1, 2, 3, 5, … × base`` per attempt, capped at ``max_delay``.

## Signature

```python
def fibonacci(base: float = 0.1, *, max_delay: float = 30.0) -> BackoffStrategy
```

## Documentation

Fibonacci-scaled delay: ``1, 2, 3, 5, … × base`` per attempt, capped at ``max_delay``.
