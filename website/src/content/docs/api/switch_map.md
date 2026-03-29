---
title: 'switch_map'
description: 'Map each outer settled value to an inner node; keep only the latest inner subscription.'
---

Map each outer settled value to an inner node; keep only the latest inner subscription.

## Signature

```python
def switch_map(
    fn: Callable[[Any], Node[Any]],
    *,
    initial: Any = _UNSET,
) -> PipeOperator
```

## Documentation

Map each outer settled value to an inner node; keep only the latest inner subscription.

On each outer ``DATA``, the previous inner is unsubscribed. Inner ``DATA`` / ``RESOLVED`` /
``DIRTY`` are forwarded; inner ``ERROR`` always terminates; inner ``COMPLETE`` completes
the output only if the outer has already completed.

Args:
    fn: ``outer_value -&gt; Node`` for the active inner.
    initial: Optional seed for :meth:`~graphrefly.core.node.Node.get` before the first inner
        emission.

Returns:
    A unary pipe operator ``(Node) -&gt; Node``.
