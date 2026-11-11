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

Wire edge groups carry a fixed set of edges from one graph to another
(R-bridge, graphrefly-py:D8):

- `wire_edge_group_out(graph, group=..., edges=...)` sends one `WireEdgeCause`
  frame per sender wave in which an edge sent.
- `frame_sink(send)` hands frames (or bytes) from a subscription to your transport.
- `frame_source(graph, open)` is the receiving end: `open(deliver)` runs at each
  activation, and each `deliver(frame)` is one wave.
- `wire_edge_group_in(graph, group=..., frames=..., edges=...)` releases each cause
  in order and at most once. Arrays for an edge whose inbound node has ended are
  skipped and the rest of the cause released; drops, gaps, refusals and such ended
  edges are values of its `issues` node.

`wire_bridge(...)` is the transport adapter over canonical protobuf bytes, with acks
and retries on explicit ack-timeout facts; `wire_bridge_ack_driver(...)` derives
those facts from a clock node you write. No timer is set.
