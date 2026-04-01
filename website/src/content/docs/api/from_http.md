---
title: 'from_http'
description: 'Create a one-shot reactive HTTP source with lifecycle tracking.'
---

Create a one-shot reactive HTTP source with lifecycle tracking.

## Signature

```python
def from_http(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: Any = None,
    transform: Callable[[Any], Any] | None = None,
    timeout_ns: int = 30_000_000_000,
    **kwargs: Any,
) -> HttpBundle
```

## Documentation

Create a one-shot reactive HTTP source with lifecycle tracking.

Uses :func:`urllib.request.urlopen` internally to remain zero-dependency.
Performs a single fetch when subscribed, then completes. For periodic
fetching, compose with ``switch_map`` and a time source.

Args:
    url: The URL to fetch.
    method: HTTP method (default ``"GET"``).
    headers: Optional request headers.
    body: Optional request body (converted to JSON if not a string).
    transform: Optional function to transform raw response bytes
        (signature: ``Callable[[bytes], Any]``). Default: ``json.loads``.
    timeout_ns: Request timeout in **nanoseconds** (default ``30s``).
    **kwargs: Passed to :func:`~graphrefly.core.node.node` as options.

Returns:
    An :class:`HttpBundle` wrapping the primary node and companions.

Example:
    ```python
    from graphrefly.extra.sources import from_http
    from graphrefly.extra.tier2 import switch_map
    from graphrefly.extra import from_timer

    # One-shot:
    api = from_http("https://api.example.com/data")

    # Periodic polling via reactive composition:
    polled = switch_map(lambda _: from_http(url))(from_timer(0, period=5.0))
    ```
Notes:
    This source is implemented with ``threading.Thread`` + ``urllib`` and does
    not currently support external cancellation signals (TS ``AbortSignal`` parity
    is deferred). Unsubscribe prevents any late emissions from being forwarded.
