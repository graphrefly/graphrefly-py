---
title: 'pausable'
description: 'Buffer ``DIRTY``/``DATA``/``RESOLVED`` while ``PAUSE`` is in effect; flush on ``RESUME``.'
---

Buffer ``DIRTY``/``DATA``/``RESOLVED`` while ``PAUSE`` is in effect; flush on ``RESUME``.

## Signature

```python
def pausable() -> PipeOperator
```

## Documentation

Buffer ``DIRTY``/``DATA``/``RESOLVED`` while ``PAUSE`` is in effect; flush on ``RESUME``.

Protocol-level pause/resume using ``PAUSE``/``RESUME`` message types. Matches
TypeScript ``pausable`` semantics.
