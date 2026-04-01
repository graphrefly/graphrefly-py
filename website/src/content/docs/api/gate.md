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

This is a value-level gate using a boolean control signal. See :func:`pausable`
for protocol-level ``PAUSE``/``RESUME`` buffering.

Args:
    control: Boolean-valued node; ``True`` lets values through, ``False`` suppresses them.

Returns:
    A unary pipe operator ``(Node) -&gt; Node``.

Example:
    ```python
    from graphrefly import state, pipe
    from graphrefly.extra.tier2 import gate
    src = state(1)
    ctrl = state(True)
    out = pipe(src, gate(ctrl))
    ```
