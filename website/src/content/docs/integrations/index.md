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

The wire bridge facades are withdrawn while the bridge model is settled in the
root repository (graphrefly-py:D5); they return on the Python bridge.
