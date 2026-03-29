---
title: 'replay'
description: 'Multicast with late-subscriber replay of last *buffer_size* DATA payloads.'
---

Multicast with late-subscriber replay of last *buffer_size* DATA payloads.

## Signature

```python
def replay[T](source: Node[T], buffer_size: int = 1) -> Node[T]
```

## Documentation

Multicast with late-subscriber replay of last *buffer_size* DATA payloads.
