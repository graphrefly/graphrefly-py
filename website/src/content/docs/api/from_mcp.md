---
title: 'from_mcp'
description: "Wrap an MCP client's server-push notifications as a reactive source."
---

Wrap an MCP client's server-push notifications as a reactive source.

## Signature

```python
def from_mcp(
    client: Any,
    *,
    method: str = "notifications/message",
    on_disconnect: Callable[[Callable[[Any], None]], None] | None = None,
    **kwargs: Any,
) -> Node[Any]
```

## Documentation

Wrap an MCP client's server-push notifications as a reactive source.

The caller owns the ``Client`` connection (``connect`` / ``close``).  ``from_mcp``
only registers a notification handler for the chosen *method* and emits each
notification payload as ``DATA``.

**Disconnect detection:** MCP SDK does not expose a built-in disconnect event.
Pass ``on_disconnect`` to wire an external signal (e.g. transport ``close`` event)
so the source can emit ``ERROR`` and tear down reactively.

Args:
    client: Any object with a ``set_notification_handler(method, handler)`` method
        (duck-typed — no SDK dependency).
    method: MCP notification method to subscribe to.  Default ``"notifications/message"``.
    on_disconnect: Optional callback ``(cb) -&gt; None`` — call ``cb(err)`` when the
        transport disconnects.

Returns:
    A :class:`~graphrefly.core.node.Node` emitting one ``DATA`` per server notification.

Example:
    ```python
    from graphrefly.extra import from_mcp
    tools = from_mcp(client, method="notifications/tools/list_changed")
    ```
