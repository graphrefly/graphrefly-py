---
title: 'sample'
description: "Emit the primary's latest ``get()`` whenever ``notifier`` settles with ``DATA``."
---

Emit the primary's latest ``get()`` whenever ``notifier`` settles with ``DATA``.

## Signature

```python
def sample(notifier: Node[Any]) -> PipeOperator
```

## Documentation

Emit the primary's latest ``get()`` whenever ``notifier`` settles with ``DATA``.

A mirror node follows the primary so ``get()`` on the output reflects the last sampled
value; the latest primary value before a sample is read via an internal pass-through node.
