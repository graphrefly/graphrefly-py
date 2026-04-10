---
title: 'keepalive'
description: "Activate a compute node's upstream wiring without a real sink."
---

Activate a compute node's upstream wiring without a real sink.

Derived/effect nodes are lazy — they don't compute until at least one
subscriber exists (COMPOSITION-GUIDE §5). ``keepalive`` subscribes with
an empty sink so the node stays wired for ``.get()`` and upstream
propagation.

Returns the unsubscribe handle.  Common usage::

    graph.add_disposer(keepalive(node))

## Signature

```python
def keepalive(n: Node[Any]) -> Any
```
