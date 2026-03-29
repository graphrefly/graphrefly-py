---
title: 'from_timer'
description: 'Like Rx ``timer``: after *delay* seconds emit *first*, then optionally tick forever.'
---

Like Rx ``timer``: after *delay* seconds emit *first*, then optionally tick forever.

## Signature

```python
def from_timer(
    delay: float,
    period: float | None = None,
    *,
    first: int = 0,
) -> Node[Any]
```

## Documentation

Like Rx ``timer``: after *delay* seconds emit *first*, then optionally tick forever.

If *period* is ``None``, emit once (value *first*) and ``COMPLETE``.
If *period* is set, emit *first*, then *first+1*, *first+2*, … every *period* seconds
(same counter shape as :func:`~graphrefly.extra.tier2.interval`).
