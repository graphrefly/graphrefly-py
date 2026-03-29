---
title: 'from_iter'
description: 'Drain a synchronous *iterable* on subscribe: one ``DATA`` per item, then ``COMPLETE``.'
---

Drain a synchronous *iterable* on subscribe: one ``DATA`` per item, then ``COMPLETE``.

## Signature

```python
def from_iter(iterable: Iterable[Any]) -> Node[Any]
```

## Documentation

Drain a synchronous *iterable* on subscribe: one ``DATA`` per item, then ``COMPLETE``.

If iteration raises, the producer emits ``ERROR`` and stops.
