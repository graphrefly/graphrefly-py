---
title: 'replay'
description: 'Multicast via :func:`share`; optional *buffer_size* reserved for future buffered replay.'
---

Multicast via :func:`share`; optional *buffer_size* reserved for future buffered replay.

## Signature

```python
def replay[T](source: Node[T], buffer_size: int | None = None) -> Node[T]
```

## Documentation

Multicast via :func:`share`; optional *buffer_size* reserved for future buffered replay.

Emits the same live stream as :func:`share`. Per-subscriber replay of historical ``DATA`` is
not implemented yet; *buffer_size* is accepted for API stability and ignored.
