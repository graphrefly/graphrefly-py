"""Core node primitives and protocol types for graphrefly."""

from graphrefly.core.meta import meta_snapshot
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
    "batch",
    "dispatch_messages",
    "emit_with_batch",
    "is_batching",
    "is_phase2_message",
    "meta_snapshot",
    "node",
    "partition_for_batch",
]
