"""GraphReFly message types, message shape aliases, and batch semantics.

Canonical message ordering (within a composite batch)
=====================================================

When multiple message types appear in a single ``down()`` call, the canonical
delivery order is determined by **signal tier**:

======  ====================  ===================  ====================================
Tier    Signals               Role                 Batch behavior
======  ====================  ===================  ====================================
0       DIRTY, INVALIDATE     Notification          Immediate (never deferred)
1       PAUSE, RESUME         Flow control          Immediate (never deferred)
2       DATA, RESOLVED        Value settlement      Deferred inside ``batch()``
3       COMPLETE, ERROR       Terminal lifecycle     Deferred to after phase-2
4       TEARDOWN              Destruction            Immediate (usually sent alone)
======  ====================  ===================  ====================================

**Rule:** Within ``emit_with_batch(strategy="partition")``, messages are partitioned
by tier and delivered in tier order. This ensures phase-2 values reach sinks
before terminal signals mark the node as done.

Unknown message types (forward-compat) are tier 0 (immediate).

Meta node bypass rules (centralized — GRAPHREFLY-SPEC §2.3)
============================================================

- **INVALIDATE** via ``graph.signal()`` — no-op on meta nodes.
- **COMPLETE / ERROR** — not propagated from parent to meta.
- **TEARDOWN** — propagated from parent to meta, releasing meta resources.
"""

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

type EmitStrategy = Literal["partition", "sequential"]
type DeferWhen = Literal["depth", "batching"]


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
    __slots__ = ("depth", "flush_in_progress", "pending", "pending_phase3")

    def __init__(self) -> None:
        self.depth = 0
        self.flush_in_progress = False
        self.pending: list[Callable[[], None]] = []
        self.pending_phase3: list[Callable[[], None]] = []


def _batch_state() -> _BatchState:
    try:
        return _batch_tls.state  # type: ignore[no-any-return]
    except AttributeError:
        bs = _BatchState()
        _batch_tls.state = bs
        return bs


_MAX_DRAIN_ITERATIONS = 1000


def _drain_pending(bs: _BatchState) -> None:
    """Run all queued deferred callbacks until the queue is quiescent.

    Drain order: phase-2 (DATA/RESOLVED) until empty, then phase-3 (meta
    companion emissions). If phase-3 callbacks enqueue new phase-2 work,
    the outer loop catches it and drains phase-2 again before re-entering
    phase-3.
    """
    owns_flush = not bs.flush_in_progress
    if owns_flush:
        bs.flush_in_progress = True
    errors: list[Exception] = []
    iterations = 0
    try:
        while bs.pending or bs.pending_phase3:
            # Phase-2: parent node settlements.
            while bs.pending:
                iterations += 1
                if iterations > _MAX_DRAIN_ITERATIONS:
                    bs.pending = []
                    bs.pending_phase3 = []
                    msg = f"batch drain exceeded {_MAX_DRAIN_ITERATIONS} iterations"
                    raise RuntimeError(msg)
                batch = bs.pending
                bs.pending = []
                for fn in batch:
                    try:
                        fn()
                    except Exception as e:
                        errors.append(e)
            # Phase-3: meta companion emissions (after parent settlement).
            if bs.pending_phase3:
                iterations += 1
                if iterations > _MAX_DRAIN_ITERATIONS:
                    bs.pending = []
                    bs.pending_phase3 = []
                    msg = f"batch drain exceeded {_MAX_DRAIN_ITERATIONS} iterations"
                    raise RuntimeError(msg)
                batch = bs.pending_phase3
                bs.pending_phase3 = []
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

    ``DIRTY`` and non-phase-2 types propagate immediately. Nested batches share
    one defer queue; flush runs only when the outermost context exits. Each
    thread has isolated batch state (GRAPHREFLY-SPEC §4.2).

    If the outermost context exits with an exception, deferred phase-2 work is
    discarded. While the drain loop is running (``flush_in_progress``), nested
    :func:`emit_with_batch` calls with ``defer_when="batching"`` still defer
    ``DATA``/``RESOLVED`` until the queue drains.

    Returns:
        A context manager that yields ``None``; has no return value.

    Example:
        ```python
        from graphrefly import state, batch
        x = state(0)
        y = state(0)
        with batch():
            x.down([("DATA", 1)])
            y.down([("DATA", 2)])
        # Downstream sees both updates atomically
        ```
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
                    bs.pending_phase3.clear()
            else:
                _drain_pending(bs)


def is_batching() -> bool:
    """Return True while inside ``batch()`` or while deferred phase-2 work is draining.

    Returns:
        ``True`` when batch depth > 0 or the drain loop is active.

    Example:
        ```python
        from graphrefly import batch, is_batching
        assert not is_batching()
        with batch():
            assert is_batching()
        ```
    """
    bs = _batch_state()
    return bs.depth > 0 or bs.flush_in_progress


def is_phase2_message(msg: Message) -> bool:
    """True for DATA and RESOLVED (phase-2 tuples deferred under batching)."""
    t = msg[0]
    return t is MessageType.DATA or t is MessageType.RESOLVED


def is_terminal_message(t: MessageType) -> bool:
    """True for COMPLETE or ERROR (tier 3 — delivered after phase-2 in the same batch)."""
    return t is MessageType.COMPLETE or t is MessageType.ERROR


def message_tier(t: MessageType) -> int:
    """Return the signal tier for a message type (see module docstring).

    0: notification (DIRTY, INVALIDATE) — immediate
    1: flow control (PAUSE, RESUME) — immediate
    2: value (DATA, RESOLVED) — deferred inside batch()
    3: terminal (COMPLETE, ERROR) — delivered after phase-2
    4: destruction (TEARDOWN) — immediate, usually alone
    0 for unknown types (forward-compat: immediate)
    """
    if t is MessageType.DIRTY or t is MessageType.INVALIDATE:
        return 0
    if t is MessageType.PAUSE or t is MessageType.RESUME:
        return 1
    if t is MessageType.DATA or t is MessageType.RESOLVED:
        return 2
    if t is MessageType.COMPLETE or t is MessageType.ERROR:
        return 3
    if t is MessageType.TEARDOWN:
        return 4
    return 0


def propagates_to_meta(t: MessageType) -> bool:
    """Whether *t* should be propagated from a parent node to its companion meta nodes.

    Only TEARDOWN propagates; COMPLETE/ERROR/INVALIDATE do not.
    """
    return t is MessageType.TEARDOWN


def partition_for_batch(messages: Messages) -> tuple[Messages, Messages, Messages]:
    """Split *messages* into three groups by signal tier.

    Returns ``(immediate, deferred, terminal)`` — tier 0-1/4, tier 2, tier 3.
    Order within each group is preserved.
    """
    immediate: Messages = []
    deferred: Messages = []
    terminal: Messages = []
    for m in messages:
        if is_phase2_message(m):
            deferred.append(m)
        elif is_terminal_message(m[0]):
            terminal.append(m)
        else:
            immediate.append(m)
    return immediate, deferred, terminal


def emit_with_batch(
    sink: Callable[[Messages], None],
    messages: Messages,
    *,
    phase: int = 2,
    strategy: EmitStrategy = "sequential",
    defer_when: DeferWhen = "batching",
    subgraph_lock: object | None = None,
) -> None:
    """Deliver *messages* to *sink* with batch-aware deferral.

    Args:
        sink: Callable receiving a :class:`~graphrefly.core.protocol.Messages` list.
        messages: The messages to deliver.
        phase: ``2`` (default) for standard DATA/RESOLVED deferral; ``3`` for
            meta companion emissions that must arrive after parent settlements.
        strategy: ``"partition"`` (default for :class:`~graphrefly.core.node.NodeImpl`)
            splits messages into immediate vs phase-2 groups; ``"sequential"`` walks
            each message in order and handles ``COMPLETE``/``ERROR`` after phase-2.
        defer_when: ``"batching"`` (default) defers phase-2 while
            :func:`is_batching` (depth or drain in progress); ``"depth"`` defers
            only while batch depth > 0.
        subgraph_lock: When set, re-acquires the subgraph write lock around deferred
            phase-2 calls to serialize batch drains with other writers.

    Example:
        ```python
        from graphrefly.core.protocol import emit_with_batch, MessageType, batch
        received = []
        sink = lambda msgs: received.extend(msgs)
        with batch():
            emit_with_batch(sink, [("DATA", 1), ("DATA", 2)])
        # Both DATA messages flushed together after batch exits
        assert len(received) == 2
        ```
    """
    if not messages:
        return
    queue_attr = "pending_phase3" if phase == 3 else "pending"
    if strategy == "partition":
        _emit_partition(sink, messages, defer_when, subgraph_lock, queue_attr)
    else:
        _emit_sequential(sink, messages, defer_when, subgraph_lock, queue_attr)


def _emit_partition(
    sink: Callable[[Messages], None],
    messages: Messages,
    defer_when: DeferWhen,
    subgraph_lock: object | None,
    queue_attr: str = "pending",
) -> None:
    bs = _batch_state()
    queue: list[Callable[[], None]] = getattr(bs, queue_attr)
    # Fast path: single-message batches (most common in graph-internal propagation)
    # skip partition_for_batch allocation entirely.
    if len(messages) == 1:
        t = messages[0][0]
        if t is MessageType.DATA or t is MessageType.RESOLVED:
            if _should_defer_phase2(bs, defer_when):

                def _emit_single() -> None:
                    sink(messages)

                queue.append(_wrap_deferred_subgraph(_emit_single, subgraph_lock))
            else:
                sink(messages)
        elif is_terminal_message(t):
            # Terminal single message: defer when batching so preceding deferred flushes first.
            if _should_defer_phase2(bs, defer_when):

                def _emit_terminal_single() -> None:
                    sink(messages)

                queue.append(_wrap_deferred_subgraph(_emit_terminal_single, subgraph_lock))
            else:
                sink(messages)
        else:
            # Immediate: emit synchronously.
            sink(messages)
        return
    # Multi-message: three-way partition by tier (see module docstring).
    immediate, deferred, terminal = partition_for_batch(messages)

    # 1. Immediate signals (tier 0-1, 4) — emit synchronously now.
    if immediate:
        sink(immediate)

    # 2. Deferred (tier 2) + Terminal (tier 3) — canonical order preserved.
    if _should_defer_phase2(bs, defer_when):
        if deferred:

            def _emit_deferred() -> None:
                sink(deferred)

            queue.append(_wrap_deferred_subgraph(_emit_deferred, subgraph_lock))
        if terminal:

            def _emit_terminal() -> None:
                sink(terminal)

            queue.append(_wrap_deferred_subgraph(_emit_terminal, subgraph_lock))
    else:
        if deferred:
            sink(deferred)
        if terminal:
            sink(terminal)


def _emit_sequential(
    sink: Callable[[Messages], None],
    messages: Messages,
    defer_when: DeferWhen,
    subgraph_lock: object | None,
    queue_attr: str = "pending",
) -> None:
    bs = _batch_state()
    queue: list[Callable[[], None]] = getattr(bs, queue_attr)
    for msg in messages:
        kind = msg[0]
        if kind in _BATCH_DEFER_TYPES and _should_defer_phase2(bs, defer_when):

            def _emit(m: Message = msg, s: Callable[[Messages], None] = sink) -> None:
                s([m])

            queue.append(_wrap_deferred_subgraph(_emit, subgraph_lock))
        elif kind in _TERMINAL_TYPES and _should_defer_phase2(bs, defer_when):
            # Terminal: defer so preceding deferred flushes first.
            def _emit_term(m: Message = msg, s: Callable[[Messages], None] = sink) -> None:
                s([m])

            queue.append(_wrap_deferred_subgraph(_emit_term, subgraph_lock))
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
    "is_terminal_message",
    "message_tier",
    "partition_for_batch",
    "propagates_to_meta",
]
