---
title: 'cached'
description: "Alias of :func:`share` with ``describe_kind='cached'`` (hot wire)."
---

Alias of :func:`share` with ``describe_kind='cached'`` (hot wire).

## Signature

```python
def cached[T](source: Node[T]) -> Node[T]
```

## Documentation

Alias of :func:`share` with ``describe_kind='cached'`` (hot wire).

Late joiners observe new ``DATA`` from the shared upstream; use
:meth:`~graphrefly.core.node.Node.get` after subscribe for the latest cached value on the
returned node.
