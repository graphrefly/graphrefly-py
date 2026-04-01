---
title: 'to_websocket'
description: 'Forward upstream DATA payloads to a WebSocket-like transport.'
---

Forward upstream DATA payloads to a WebSocket-like transport.

## Signature

```python
def to_websocket(
    source: Node[Any],
    socket: Any | None = None,
    *,
    send: Callable[[Any], None] | None = None,
    close: Callable[..., None] | None = None,
    serialize: Callable[[Any], Any] | None = None,
    close_on_complete: bool = True,
    close_on_error: bool = True,
    close_code: int | None = None,
    close_reason: str | None = None,
    on_transport_error: Callable[[dict[str, Any]], None] | None = None,
) -> Callable[[], None]
```

## Documentation

Forward upstream DATA payloads to a WebSocket-like transport.

Transport failures from serialization/send/close are reported through
``on_transport_error`` as a dict with ``stage``, ``error``, and ``message`` keys.
