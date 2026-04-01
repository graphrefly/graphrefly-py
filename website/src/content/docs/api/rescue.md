---
title: 'rescue'
description: 'Turn upstream ``ERROR`` into a ``DATA`` value produced by ``recover(exc)``.'
---

Turn upstream ``ERROR`` into a ``DATA`` value produced by ``recover(exc)``.

## Signature

```python
def rescue(recover: Callable[[BaseException], Any]) -> PipeOperator
```

## Documentation

Turn upstream ``ERROR`` into a ``DATA`` value produced by ``recover(exc)``.

Args:
    recover: Callable ``(exception) -&gt; value`` whose return is emitted as ``DATA``.

Returns:
    A unary pipe operator ``(Node) -&gt; Node``.

Example:
    ```python
    from graphrefly.extra import throw_error
    from graphrefly.extra.tier2 import rescue
    from graphrefly.extra.sources import first_value_from
    n = rescue(lambda e: -1)(throw_error(ValueError("oops")))
    assert first_value_from(n) == -1
    ```
