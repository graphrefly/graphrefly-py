---
title: 'from_websocket'
description: 'Bridge WebSocket events into a GraphReFly source.'
---

Bridge WebSocket events into a GraphReFly source.

## Signature

```python
def from_websocket(
    socket: Any | None = None,
    *,
    register: Callable[
        [
            Callable[[Any], None],
            Callable[[BaseException | Any], None],
            Callable[[], None],
        ],
        Callable[[], None] | None,
    ]
    | None = None,
    add_method: str = "add_listener",
    remove_method: str = "remove_listener",
    message_event: str = "message",
    error_event: str = "error",
    close_event: str = "close",
    parse: Callable[[Any], Any] | None = None,
    close_on_cleanup: bool = False,
) -> Node[Any]
```

## Documentation

Bridge WebSocket events into a GraphReFly source.

You can either pass a ``register`` callback (preferred in Python for runtime-agnostic wiring)
or pass a socket-like object with ``add_method``/``remove_method`` listener APIs.

The ``register`` callback must be atomic: either fully register and return a cleanup callable,
or raise before any listener side effects.
