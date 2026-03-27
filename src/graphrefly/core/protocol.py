"""GraphReFly message types, message shape aliases, and batch semantics."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

# ---------------------------------------------------------------------------
# Message vocabulary — GRAPHREFLY-SPEC § 1.2, Appendix A
# ---------------------------------------------------------------------------


class MessageType(StrEnum):
    """Wire discriminator for node messages (always the first tuple element)."""

    DATA = "DATA"
    DIRTY = "DIRTY"
    RESOLVED = "RESOLVED"
    INVALIDATE = "INVALIDATE"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    TEARDOWN = "TEARDOWN"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"


# Tuple with payload, or single-element tuple for type-only messages (e.g. DIRTY).
type Message = tuple[MessageType, Any] | tuple[MessageType]
type Messages = list[Message]

# Phase-2 messages deferred by batch(); DIRTY and other signals flush immediately.
_BATCH_DEFER_TYPES: frozenset[MessageType] = frozenset({MessageType.DATA, MessageType.RESOLVED})

# Terminals (GRAPHREFLY-SPEC § 1.3 #4): flush deferred phase-2 first so they are not observed after.
_TERMINAL_TYPES: frozenset[MessageType] = frozenset({MessageType.COMPLETE, MessageType.ERROR})

# ---------------------------------------------------------------------------
# Batch — defers DATA/RESOLVED; DIRTY propagates immediately (GRAPHREFLY-SPEC § 1.3 #7)
# ---------------------------------------------------------------------------

_batch_tls = threading.local()


class _BatchState:
    __slots__ = ("depth", "flush_in_progress", "pending")

    def __init__(self) -> None:
        self.depth = 0
        self.flush_in_progress = False
        self.pending: list[Callable[[], None]] = []


def _batch_state() -> _BatchState:
    bs: _BatchState | None = getattr(_batch_tls, "state", None)
    if bs is None:
        bs = _BatchState()
        _batch_tls.state = bs
    return bs


def _drain_pending(bs: _BatchState) -> None:
    """Run all queued deferred callbacks until the queue is quiescent."""
    errors: list[Exception] = []
    while bs.pending:
        batch = bs.pending
        bs.pending = []
        for fn in batch:
            try:
                fn()
            except Exception as e:
                errors.append(e)
    if len(errors) == 1:
        raise errors[0]
    if len(errors) > 1:
        raise ExceptionGroup("batch drain", errors)


@contextmanager
def batch() -> Generator[None]:
    """Defer phase-2 messages (DATA, RESOLVED) until the outermost batch exits.

    DIRTY and non-phase-2 types propagate immediately. Nested batches share one
    defer queue; flush runs only when the outermost context exits.

    Each thread has isolated batch state (GRAPHREFLY-SPEC § 4.2).

    While deferred work is running, :func:`is_batching` remains true so nested
    ``dispatch_messages`` calls still defer DATA/RESOLVED until the queue drains.
    """
    bs = _batch_state()
    bs.depth += 1
    try:
        yield
    finally:
        bs.depth -= 1
        if bs.depth == 0:
            owns_flush = not bs.flush_in_progress
            if owns_flush:
                bs.flush_in_progress = True
            try:
                _drain_pending(bs)
            finally:
                if owns_flush:
                    bs.flush_in_progress = False


def is_batching() -> bool:
    """True while inside ``batch()`` *or* while deferred phase-2 work is draining."""
    bs = _batch_state()
    return bs.depth > 0 or bs.flush_in_progress


def dispatch_messages(messages: Messages, sink: Callable[[Messages], None]) -> None:
    """Deliver *messages* to *sink*, applying batch deferral per message.

    For each message tuple, DATA and RESOLVED are deferred while :func:`is_batching`;
    DIRTY and other non-terminal types call *sink* immediately with a one-message list.

    COMPLETE and ERROR (GRAPHREFLY-SPEC § 1.3 #4) flush any deferred DATA/RESOLVED on
    this thread before the terminal is delivered, so terminals are not observed before
    deferred phase-2 messages from the same logical ``down()`` array.
    """
    for msg in messages:
        kind = msg[0]
        if kind in _TERMINAL_TYPES:
            _drain_pending(_batch_state())
        if kind in _BATCH_DEFER_TYPES and is_batching():
            bs = _batch_state()

            def _emit(m: Message = msg, s: Callable[[Messages], None] = sink) -> None:
                s([m])

            bs.pending.append(_emit)
        else:
            sink([msg])


__all__ = [
    "Message",
    "MessageType",
    "Messages",
    "batch",
    "dispatch_messages",
    "is_batching",
]
