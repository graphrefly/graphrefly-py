"""graphrefly — Reactive graph protocol for human and LLM co-operation."""

from graphrefly.core import (
    Message,
    Messages,
    MessageType,
    batch,
    dispatch_messages,
    is_batching,
)

__version__ = "0.1.0"

__all__ = [
    "Message",
    "MessageType",
    "Messages",
    "__version__",
    "batch",
    "dispatch_messages",
    "is_batching",
]
