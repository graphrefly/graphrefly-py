---
title: 'emit_with_batch'
description: 'Deliver *messages* to *sink* with batch-aware phase-2 deferral.'
---

Deliver *messages* to *sink* with batch-aware phase-2 deferral.

## Signature

```python
def emit_with_batch(
    sink: Callable[[Messages], None],
    messages: Messages,
    *,
    strategy: EmitStrategy = "sequential",
    defer_when: DeferWhen = "batching",
    subgraph_lock: object | None = None,
) -> None
```

## Documentation

Deliver *messages* to *sink* with batch-aware phase-2 deferral.

**Strategies** (single implementation; see ``docs/optimizations.md``):

- ``strategy="partition"`` — graphrefly-ts ``emitWithBatch``: split the array
  into immediate vs phase-2 groups; emit immediate once, then defer or emit
  the phase-2 block. Used by :class:`~graphrefly.core.node.NodeImpl` ``down``.

- ``strategy="sequential"`` — walk tuples in order; each COMPLETE/ERROR drains
  pending phase-2 first (spec §1.3 #4). Used by tests and low-level protocol
  helpers.

**Defer predicate** (when to queue DATA/RESOLVED instead of calling *sink*):

- ``defer_when="batching"`` — defer while :func:`is_batching` (depth **or**
  flush-in-progress). Matches historical ``dispatch_messages`` / nested-drain QA.

- ``defer_when="depth"`` — defer only while ``batch`` depth &gt; 0 (not while
  draining). Matches TS ``emitWithBatch`` / node hot path so nested work during
  flush does not re-defer.

**Concurrency:** when *subgraph_lock* is the owning node (or any registry member
in the same component), deferred phase-2 deliveries re-acquire
:func:`~graphrefly.core.subgraph_locks.acquire_subgraph_write_lock_with_defer`
around the sink call so batch drains stay serialized with other writers (roadmap 0.4).
