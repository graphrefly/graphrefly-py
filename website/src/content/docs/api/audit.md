---
title: 'audit'
description: 'Trailing-only window: after each ``DATA``, wait ``seconds``, then emit the latest value.'
---

Trailing-only window: after each ``DATA``, wait ``seconds``, then emit the latest value.

## Signature

```python
def audit(seconds: float) -> PipeOperator
```

## Documentation

Trailing-only window: after each ``DATA``, wait ``seconds``, then emit the latest value.

Each ``DATA`` stores the latest value and restarts the timer. When the timer fires,
the stored value is emitted. No leading-edge emission (Rx ``auditTime`` semantics).
