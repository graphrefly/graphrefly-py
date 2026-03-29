---
title: 'debounce'
description: 'Emit the latest upstream ``DATA`` only after ``seconds`` of silence; flush on ``COMPLETE``.'
---

Emit the latest upstream ``DATA`` only after ``seconds`` of silence; flush on ``COMPLETE``.

## Signature

```python
def debounce(seconds: float) -> PipeOperator
```

## Documentation

Emit the latest upstream ``DATA`` only after ``seconds`` of silence; flush on ``COMPLETE``.

Timer is cancelled on upstream ``ERROR`` or unsubscribe.
