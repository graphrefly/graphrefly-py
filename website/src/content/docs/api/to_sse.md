---
title: 'to_sse'
description: 'Convert node messages into standard SSE text frames.'
---

Convert node messages into standard SSE text frames.

## Signature

```python
def to_sse(
    source: Node[Any],
    *,
    serialize: Callable[[Any], str] | None = None,
    data_event: str = "data",
    error_event: str = "error",
    complete_event: str = "complete",
    include_resolved: bool = False,
    include_dirty: bool = False,
    keepalive_s: float | None = None,
    cancel_event: threading.Event | None = None,
    event_name_resolver: Callable[[Any], str] | None = None,
) -> Iterator[str]
```

## Documentation

Convert node messages into standard SSE text frames.

This is a sink adapter implemented as a thin subscription bridge over GraphReFly
messages. The returned iterator yields framed SSE chunks (``event: ...`` and
``data: ...`` lines, separated by a blank line).
