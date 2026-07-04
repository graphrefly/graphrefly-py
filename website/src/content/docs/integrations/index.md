---
title: "Integrations"
description: "Python runtime and adapter integration surfaces."
---

Python integrations are host-owned. The package exposes typed facades and
adapter boundaries, but it does not own your event loop, HTTP client, retry
policy, or process manager.

## Async runtimes

- `asyncio_runner()`
- `trio_runner(nursery)`
- `anyio_runner(task_group)`

## Network sources

- `from_http(...)` accepts a host-owned `LocalHttpDriver`.
- `from_sse(...)` accepts a host-owned `LocalHttpStreamDriver`.

Both helpers keep IO at the boundary and re-enter the graph through declared
source behavior.

## Cross-graph transport

Wire bridge helpers expose high-level Python facades over the native bridge
semantics:

- `wire_bridge(...)`
- `wire_bridge_ack_driver(...)`
- `wire_bridge_protobuf(...)`
- `wire_edge_group(...)`

See the generated API Reference for constructor signatures and status types.
