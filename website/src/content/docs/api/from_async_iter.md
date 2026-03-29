---
title: 'from_async_iter'
description: 'Iterate an async iterable on a worker thread; ``DATA`` per item, then ``COMPLETE``.'
---

Iterate an async iterable on a worker thread; ``DATA`` per item, then ``COMPLETE``.

## Signature

```python
def from_async_iter(aiterable: AsyncIterable[Any]) -> Node[Any]
```

## Documentation

Iterate an async iterable on a worker thread; ``DATA`` per item, then ``COMPLETE``.
