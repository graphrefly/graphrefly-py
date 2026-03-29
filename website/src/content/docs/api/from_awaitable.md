---
title: 'from_awaitable'
description: 'Resolve an awaitable on a worker thread; one ``DATA`` then ``COMPLETE``, or ``ERROR``.'
---

Resolve an awaitable on a worker thread; one ``DATA`` then ``COMPLETE``, or ``ERROR``.

## Signature

```python
def from_awaitable(awaitable: Awaitable[Any]) -> Node[Any]
```

## Documentation

Resolve an awaitable on a worker thread; one ``DATA`` then ``COMPLETE``, or ``ERROR``.
