---
title: 'pipe'
description: 'Compose a linear pipeline of unary operators over ``source``.'
---

Compose a linear pipeline of unary operators over ``source``.

## Signature

```python
def pipe(source: Node[Any], *ops: PipeOperator) -> Node[Any]
```

## Documentation

Compose a linear pipeline of unary operators over ``source``.

Args:
    source: The root node to pipe through operators.
    *ops: Unary operator callables each transforming ``Node -&gt; Node``.

Returns:
    The last node in the pipeline (result of applying all operators in order).

Example:
    ```python
    from graphrefly import state, pipe
    from graphrefly.extra import map_val, filter_val
    x = state(0)
    result = pipe(x, map_val(lambda v: v * 2), filter_val(lambda v: v &gt; 0))
    ```
