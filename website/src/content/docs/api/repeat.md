---
title: 'repeat'
description: 'Play the source to ``COMPLETE``, then re-subscribe, ``times`` times total.'
---

Play the source to ``COMPLETE``, then re-subscribe, ``times`` times total.

## Signature

```python
def repeat(times: int) -> PipeOperator
```

## Documentation

Play the source to ``COMPLETE``, then re-subscribe, ``times`` times total.

Each pass ends when the source emits ``COMPLETE``; the operator then subscribes again
until ``times`` passes have finished.
