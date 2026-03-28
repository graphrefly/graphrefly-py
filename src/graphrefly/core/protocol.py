"""GraphReFly message types, message shape aliases, and batch semantics."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable  # noqa: TC003 — runtime type for _wrap_deferred_subgraph
from contextlib import contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Generator

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

EmitStrategy = Literal["partition", "sequential"]
DeferWhen = Literal["depth", "batching"]


def _wrap_deferred_subgraph(
    fn: Callable[[], None],
    subgraph_lock: object | None,
) -> Callable[[], None]:
    """Re-acquire subgraph write lock when running deferred phase-2 (batch drain)."""
    if subgraph_lock is None:
        return fn
    from graphrefly.core.subgraph_locks import acquire_subgraph_write_lock_with_defer

    def wrapped() -> None:
        with acquire_subgraph_write_lock_with_defer(subgraph_lock):
            fn()

    return wrapped


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
    owns_flush = not bs.flush_in_progress
    if owns_flush:
        bs.flush_in_progress = True
    errors: list[Exception] = []
    try:
        while bs.pending:
            batch = bs.pending
            bs.pending = []
            for fn in batch:
                try:
                    fn()
                except Exception as e:
                    errors.append(e)
    finally:
        if owns_flush:
            bs.flush_in_progress = False
    if len(errors) == 1:
        raise errors[0]
    if len(errors) > 1:
        raise ExceptionGroup("batch drain", errors)


def _should_defer_phase2(bs: _BatchState, defer_when: DeferWhen) -> bool:
    if defer_when == "depth":
        return bs.depth > 0
    return bs.depth > 0 or bs.flush_in_progress


@contextmanager
def batch() -> Generator[None]:
    """Defer phase-2 messages (DATA, RESOLVED) until the outermost batch exits.

    DIRTY and non-phase-2 types propagate immediately. Nested batches share one
    defer queue; flush runs only when the outermost context exits.

    If the outermost context exits with an exception, deferred phase-2 work for
    that frame is discarded — phase-2 is not flushed after an error. While the
    drain loop is running (``flush_in_progress``), a nested ``batch()`` that
    throws must **not** clear the global queue (cross-language decision A4;
    matches graphrefly-ts ``batch``).

    Each thread has isolated batch state (GRAPHREFLY-SPEC § 4.2).

    While deferred work is running, :func:`is_batching` remains true so nested
    :func:`emit_with_batch` calls (``defer_when="batching"``) still defer
    DATA/RESOLVED until the queue drains.
    """
    bs = _batch_state()
    bs.depth += 1
    try:
        yield
    finally:
        bs.depth -= 1
        if bs.depth == 0:
            if sys.exc_info()[1] is not None:
                if not bs.flush_in_progress:
                    bs.pending.clear()
            else:
                _drain_pending(bs)


def is_batching() -> bool:
    """True while inside ``batch()`` *or* while deferred phase-2 work is draining."""
    bs = _batch_state()
    return bs.depth > 0 or bs.flush_in_progress


def is_phase2_message(msg: Message) -> bool:
    """True for DATA and RESOLVED (phase-2 tuples deferred under batching)."""
    return msg[0] in _BATCH_DEFER_TYPES


def partition_for_batch(messages: Messages) -> tuple[Messages, Messages]:
    """Split *messages* into immediate vs phase-2 tuples (graphrefly-ts ``partitionForBatch``)."""
    immediate: Messages = []
    deferred: Messages = []
    for m in messages:
        if is_phase2_message(m):
            deferred.append(m)
        else:
            immediate.append(m)
    return immediate, deferred


def emit_with_batch(
    sink: Callable[[Messages], None],
    messages: Messages,
    *,
    strategy: EmitStrategy = "sequential",
    defer_when: DeferWhen = "batching",
    subgraph_lock: object | None = None,
) -> None:
    """Deliver *messages* to *sink* with batch-aware phase-2 deferral.

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

    - ``defer_when="depth"`` — defer only while ``batch`` depth > 0 (not while
      draining). Matches TS ``emitWithBatch`` / node hot path so nested work during
      flush does not re-defer.

    **Concurrency:** when *subgraph_lock* is the owning node (or any registry member
    in the same component), deferred phase-2 deliveries re-acquire
    :func:`~graphrefly.core.subgraph_locks.acquire_subgraph_write_lock_with_defer`
    around the sink call so batch drains stay serialized with other writers (roadmap 0.4).
    """
    if not messages:
        return
    if strategy == "partition":
        _emit_partition(sink, messages, defer_when, subgraph_lock)
    else:
        _emit_sequential(sink, messages, defer_when, subgraph_lock)


def _emit_partition(
    sink: Callable[[Messages], None],
    messages: Messages,
    defer_when: DeferWhen,
    subgraph_lock: object | None,
) -> None:
    immediate, deferred = partition_for_batch(messages)
    bs = _batch_state()
    if immediate:
        sink(immediate)
    if not deferred:
        return
    if _should_defer_phase2(bs, defer_when):

        def _emit() -> None:
            sink(deferred)

        bs.pending.append(_wrap_deferred_subgraph(_emit, subgraph_lock))
    else:
        sink(deferred)


def _emit_sequential(
    sink: Callable[[Messages], None],
    messages: Messages,
    defer_when: DeferWhen,
    subgraph_lock: object | None,
) -> None:
    bs = _batch_state()
    for msg in messages:
        kind = msg[0]
        if kind in _TERMINAL_TYPES:
            _drain_pending(bs)
        if kind in _BATCH_DEFER_TYPES and _should_defer_phase2(bs, defer_when):

            def _emit(m: Message = msg, s: Callable[[Messages], None] = sink) -> None:
                s([m])

            bs.pending.append(_wrap_deferred_subgraph(_emit, subgraph_lock))
        else:
            sink([msg])


def dispatch_messages(messages: Messages, sink: Callable[[Messages], None]) -> None:
    """Backward-compatible alias: ``emit_with_batch(sink, messages)`` with defaults."""
    emit_with_batch(sink, messages)


__all__ = [
    "DeferWhen",
    "EmitStrategy",
    "Message",
    "MessageType",
    "Messages",
    "batch",
    "dispatch_messages",
    "emit_with_batch",
    "is_batching",
    "is_phase2_message",
    "partition_for_batch",
]
