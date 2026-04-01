---
title: 'repeat'
description: 'Play the source to ``COMPLETE``, then re-subscribe, repeating ``times`` passes total.'
---

Play the source to ``COMPLETE``, then re-subscribe, repeating ``times`` passes total.

## Signature

```python
def repeat(times: int) -> PipeOperator
```

## Documentation

Play the source to ``COMPLETE``, then re-subscribe, repeating ``times`` passes total.

Each pass ends when the source emits ``COMPLETE``; the operator then
subscribes again until all passes have finished.

Args:
    times: Total number of source passes to play through.

Returns:
    A unary pipe operator ``(Node) -&gt; Node``.

Example:
    ```python
    from graphrefly.extra import of
    from graphrefly.extra.tier2 import repeat
    from graphrefly.extra.sources import to_list
    assert to_list(repeat(3)(of(1))) == [1, 1, 1]
    ```
