---
title: 'emit_with_batch'
description: 'Deliver *messages* to *sink* with batch-aware deferral.'
---

Deliver *messages* to *sink* with batch-aware deferral.

## Signature

```python
def emit_with_batch(
    sink: Callable[[Messages], None],
    messages: Messages,
    *,
    phase: int = 2,
    strategy: EmitStrategy = "sequential",
    defer_when: DeferWhen = "batching",
    subgraph_lock: object | None = None,
) -> None
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `sink` | Callable receiving a :class:`~graphrefly.core.protocol.Messages` list. |
| `messages` | The messages to deliver. |
| `phase` | ``2`` (default) for standard DATA/RESOLVED deferral; ``3`` for meta companion emissions that must arrive after parent settlements. |
| `strategy` | ``"partition"`` (default for :class:`~graphrefly.core.node.NodeImpl`) splits messages into immediate vs phase-2 groups; ``"sequential"`` walks each message in order and handles ``COMPLETE``/``ERROR`` after phase-2. |
| `defer_when` | ``"batching"`` (default) defers phase-2 while :func:`is_batching` (depth or drain in progress); ``"depth"`` defers only while batch depth &gt; 0. |
| `subgraph_lock` | When set, re-acquires the subgraph write lock around deferred phase-2 calls to serialize batch drains with other writers. |

## Basic Usage

```python
from graphrefly.core.protocol import emit_with_batch, MessageType, batch
received = []
sink = lambda msgs: received.extend(msgs)
with batch():
    emit_with_batch(sink, [("DATA", 1), ("DATA", 2)])
# Both DATA messages flushed together after batch exits
assert len(received) == 2
```
