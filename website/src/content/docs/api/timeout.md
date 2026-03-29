---
title: 'timeout'
description: 'Emit ``ERROR`` if no ``DATA`` within ``seconds`` after subscribe or last ``DATA``.'
---

Emit ``ERROR`` if no ``DATA`` within ``seconds`` after subscribe or last ``DATA``.

## Signature

```python
def timeout(seconds: float, *, error: BaseException | None = None) -> PipeOperator
```

## Documentation

Emit ``ERROR`` if no ``DATA`` within ``seconds`` after subscribe or last ``DATA``.

Timer resets on each ``DATA``; unsubscribe cancels the watchdog.
