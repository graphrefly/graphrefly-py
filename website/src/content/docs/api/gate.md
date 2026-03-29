---
title: 'gate'
description: 'Forward ``DATA`` only when ``control`` is truthy; otherwise emit ``RESOLVED``.'
---

Forward ``DATA`` only when ``control`` is truthy; otherwise emit ``RESOLVED``.

## Signature

```python
def gate(control: Node[Any]) -> PipeOperator
```

## Documentation

Forward ``DATA`` only when ``control`` is truthy; otherwise emit ``RESOLVED``.

This is a value-level gate (boolean control signal).  See :func:`pausable` for
a protocol-level ``PAUSE``/``RESUME`` buffer.
