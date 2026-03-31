---
title: 'from_webhook'
description: 'Bridge HTTP webhook callbacks into a GraphReFly source.'
---

Bridge HTTP webhook callbacks into a GraphReFly source.

## Signature

```python
def from_webhook(
    register: Callable[
        [
            Callable[[Any], None],
            Callable[[BaseException | Any], None],
            Callable[[], None],
        ],
        Callable[[], None] | None,
    ],
) -> Node[Any]
```
