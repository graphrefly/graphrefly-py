"""Core node primitives and protocol types for graphrefly."""

from graphrefly.core.meta import describe_node, meta_snapshot
from graphrefly.core.node import (
    Node,
    NodeActions,
    NodeFn,
    NodeImpl,
    NodeStatus,
    SubscribeHints,
    node,
)
from graphrefly.core.protocol import (
    DeferWhen,
    EmitStrategy,
    Message,
    Messages,
    MessageType,
    batch,
    dispatch_messages,
    emit_with_batch,
    is_batching,
    is_phase2_message,
    partition_for_batch,
)
from graphrefly.core.subgraph_locks import (
    acquire_subgraph_write_lock,
    acquire_subgraph_write_lock_with_defer,
    defer_down,
    defer_set,
    ensure_registered,
    union_nodes,
)

__all__ = [
    "DeferWhen",
    "EmitStrategy",
    "Message",
    "MessageType",
    "Messages",
    "Node",
    "NodeActions",
    "NodeFn",
    "NodeImpl",
    "NodeStatus",
    "SubscribeHints",
    "acquire_subgraph_write_lock",
    "acquire_subgraph_write_lock_with_defer",
    "batch",
    "defer_down",
    "defer_set",
    "describe_node",
    "dispatch_messages",
    "emit_with_batch",
    "ensure_registered",
    "is_batching",
    "is_phase2_message",
    "meta_snapshot",
    "node",
    "partition_for_batch",
    "union_nodes",
]
