"""Core node primitives and protocol types for graphrefly."""

from graphrefly.core.protocol import (
    Message,
    Messages,
    MessageType,
    batch,
    dispatch_messages,
    is_batching,
)

__all__ = [
    "Message",
    "MessageType",
    "Messages",
    "batch",
    "dispatch_messages",
    "is_batching",
]
