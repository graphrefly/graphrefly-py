---
title: 'window_count'
description: 'Split source ``DATA`` into sub-node windows of ``n`` items each.'
---

Split source ``DATA`` into sub-node windows of ``n`` items each.

## Signature

```python
def window_count(n: int) -> PipeOperator
```

## Documentation

Split source ``DATA`` into sub-node windows of ``n`` items each.

Args:
    n: Number of ``DATA`` values per sub-node window.

Returns:
    A unary pipe operator ``(Node) -&gt; Node[Node]``.

Example:
    ```python
    from graphrefly import state, pipe
    from graphrefly.extra.tier2 import window_count
    src = state(0)
    out = pipe(src, window_count(3))
    ```
