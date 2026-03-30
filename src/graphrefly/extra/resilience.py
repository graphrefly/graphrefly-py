"""Resilience utilities — roadmap §3.1 (retry, breaker, rate limit, status companions)."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from graphrefly.core.node import Node, NodeActions, node
from graphrefly.core.protocol import Messages, MessageType, batch
from graphrefly.extra.backoff import (
    BackoffPreset,
    BackoffStrategy,
    resolve_backoff_preset,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "TokenBucket",
    "WithBreakerBundle",
    "WithStatusBundle",
    "circuit_breaker",
    "rate_limiter",
    "retry",
    "token_bucket",
    "token_tracker",
    "with_breaker",
    "with_status",
]


def _msg_val(m: tuple[Any, ...]) -> Any:
    assert len(m) >= 2
    return m[1]


def _coerce_delay(raw_delay: Any) -> float:
    try:
        delay = float(raw_delay)
    except (TypeError, ValueError) as e:
        msg = "backoff strategy must return a number or None"
        raise ValueError(msg) from e
    if not math.isfinite(delay):
        msg = "backoff strategy returned non-finite delay"
        raise ValueError(msg)
    return 0.0 if delay < 0 else delay


def retry(
    source: Node[Any],
    count: int | None = None,
    *,
    backoff: BackoffStrategy | BackoffPreset | None = None,
) -> Node[Any]:
    """Retry upstream after ``ERROR`` with optional backoff (threading timer between attempts).

    Unsubscribes from the source after each terminal ``ERROR``, waits (possibly zero), then
    resubscribes. Successful ``DATA`` resets the attempt counter.

    For a useful retry after upstream ``ERROR``, the source should be
    :func:`~graphrefly.core.node.node` with ``resubscribable=True`` so a new subscription can
    deliver again after a terminal error.
    """
    max_retries = count if count is not None else (0 if backoff is None else 2_147_483_647)
    if max_retries < 0:
        msg = "count must be >= 0"
        raise ValueError(msg)

    strategy: BackoffStrategy | None = (
        resolve_backoff_preset(backoff) if isinstance(backoff, str) else backoff
    )

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        attempt = [0]
        stopped = [False]
        prev_delay: list[float | None] = [None]
        upstream_unsub: list[Callable[[], None] | None] = [None]
        timer_holder: list[threading.Timer | None] = [None]
        timer_generation = [0]
        lock = threading.Lock()

        def cancel_timer() -> None:
            if timer_holder[0] is not None:
                timer_holder[0].cancel()
                timer_holder[0] = None

        def disconnect_upstream() -> None:
            if upstream_unsub[0] is not None:
                upstream_unsub[0]()
                upstream_unsub[0] = None

        def connect() -> None:
            cancel_timer()
            disconnect_upstream()

            def sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.DATA:
                        attempt[0] = 0
                        prev_delay[0] = None
                        actions.emit(_msg_val(m))
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        disconnect_upstream()
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        schedule_retry_or_finish(_msg_val(m))
                        return
                    else:
                        actions.down([m])

            upstream_unsub[0] = source.subscribe(sink)

        def schedule_retry_or_finish(err: BaseException) -> None:
            with lock:
                if stopped[0]:
                    return
                if attempt[0] >= max_retries:
                    disconnect_upstream()
                    actions.down([(MessageType.ERROR, err)])
                    return

                raw = 0.0 if strategy is None else strategy(attempt[0], err, prev_delay[0])
                delay = _coerce_delay(0.0 if raw is None else raw)
                prev_delay[0] = delay
                attempt[0] += 1
                timer_generation[0] += 1
                current_gen = timer_generation[0]
                disconnect_upstream()

            safe_delay = delay if delay > 0 else 1e-6

            def fire() -> None:
                with lock:
                    if stopped[0] or current_gen != timer_generation[0]:
                        return
                connect()

            tt = threading.Timer(safe_delay, fire)
            tt.daemon = True
            tt.start()
            timer_holder[0] = tt

        connect()

        def cleanup() -> None:
            with lock:
                stopped[0] = True
                timer_generation[0] += 1
            cancel_timer()
            disconnect_upstream()

        return cleanup

    return node(
        start,
        describe_kind="operator",
        complete_when_deps_complete=False,
        initial=source.get(),
    )


CircuitState = Literal["closed", "open", "half-open"]


class CircuitOpenError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Circuit breaker is open")


@runtime_checkable
class CircuitBreaker(Protocol):
    """Protocol for circuit breaker instances (use :func:`circuit_breaker` to create)."""

    @property
    def state(self) -> CircuitState: ...
    @property
    def failure_count(self) -> int: ...
    def can_execute(self) -> bool: ...
    def record_success(self) -> None: ...
    def record_failure(self, _error: BaseException | None = None) -> None: ...
    def reset(self) -> None: ...


class _CircuitBreakerImpl:
    """Thread-safe circuit breaker (closed / open / half-open) with optional cooldown escalation."""

    __slots__ = (
        "_cooldown_base",
        "_cooldown_strategy",
        "_failures",
        "_half_open_max",
        "_last_cooldown",
        "_lock",
        "_open_cycle",
        "_opened_at",
        "_state",
        "_threshold",
        "_trials",
    )

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        cooldown: float = 30.0,
        cooldown_strategy: BackoffStrategy | None = None,
        half_open_max: int = 1,
    ) -> None:
        self._threshold = max(1, failure_threshold)
        self._cooldown_base = max(0.0, cooldown)
        self._cooldown_strategy = cooldown_strategy
        self._half_open_max = max(1, half_open_max)
        self._state: CircuitState = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._trials = 0
        self._open_cycle = 0
        self._last_cooldown = self._cooldown_base
        self._lock = threading.Lock()

    def _get_cooldown(self) -> float:
        if self._cooldown_strategy is None:
            return self._cooldown_base
        raw = self._cooldown_strategy(self._open_cycle, None, None)
        return float(raw) if raw is not None else self._cooldown_base

    def _transition_to_open(self) -> None:
        self._state = "open"
        self._last_cooldown = self._get_cooldown()
        self._opened_at = time.monotonic()
        self._trials = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failures

    def can_execute(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if (time.monotonic() - self._opened_at) >= self._last_cooldown:
                    self._state = "half-open"
                    self._trials = 1
                    return True
                return False
            if self._trials < self._half_open_max:
                self._trials += 1
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            if self._state == "half-open":
                self._state = "closed"
                self._failures = 0
                self._open_cycle = 0
            elif self._state == "closed":
                self._failures = 0

    def record_failure(self, _error: BaseException | None = None) -> None:
        with self._lock:
            if self._state == "half-open":
                self._open_cycle += 1
                self._transition_to_open()
                return
            self._failures += 1
            if self._failures >= self._threshold:
                self._transition_to_open()

    def reset(self) -> None:
        with self._lock:
            self._state = "closed"
            self._failures = 0
            self._open_cycle = 0
            self._trials = 0


def circuit_breaker(
    *,
    failure_threshold: int = 5,
    cooldown: float = 30.0,
    cooldown_strategy: BackoffStrategy | None = None,
    half_open_max: int = 1,
) -> CircuitBreaker:
    """Thread-safe circuit breaker (closed / open / half-open).

    Supports escalating cooldown via an optional backoff strategy.

    Args:
        failure_threshold: Failures before opening (default ``5``).
        cooldown: Base cooldown seconds (default ``30.0``).
        cooldown_strategy: Backoff for cooldown escalation.
        half_open_max: Trials in half-open (default ``1``).
    """
    return _CircuitBreakerImpl(
        failure_threshold=failure_threshold,
        cooldown=cooldown,
        cooldown_strategy=cooldown_strategy,
        half_open_max=half_open_max,
    )


@dataclass(frozen=True, slots=True)
class WithBreakerBundle:
    """Result of :func:`with_breaker`: main output and a ``state`` companion node."""

    node: Node[Any]
    breaker_state: Node[str]


def with_breaker(
    breaker: CircuitBreaker,
    *,
    on_open: Literal["skip", "error"] = "skip",
) -> Callable[[Node[Any]], WithBreakerBundle]:
    """Guard a source with a :class:`CircuitBreaker`.

    On each upstream ``DATA``, if the breaker refuses work, either emit ``RESOLVED`` (*skip*)
    or ``ERROR`` (:exc:`CircuitOpenError`) (*error*). ``COMPLETE`` records success; ``ERROR``
    records failure and is forwarded.

    Companion ``breaker_state`` is wired into ``node.meta`` so it appears in ``describe()``.
    """

    def _op(src: Node[Any]) -> WithBreakerBundle:
        def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
            def sync_state() -> None:
                out.meta["breaker_state"].down([(MessageType.DATA, breaker.state)])

            def sink(msgs: Messages) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DIRTY:
                        actions.down([(MessageType.DIRTY,)])
                    elif t is MessageType.DATA:
                        if breaker.can_execute():
                            sync_state()
                            actions.emit(_msg_val(m))
                        else:
                            sync_state()
                            if on_open == "error":
                                actions.down([(MessageType.ERROR, CircuitOpenError())])
                            else:
                                actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.RESOLVED:
                        actions.down([(MessageType.RESOLVED,)])
                    elif t is MessageType.COMPLETE:
                        breaker.record_success()
                        sync_state()
                        actions.down([(MessageType.COMPLETE,)])
                    elif t is MessageType.ERROR:
                        breaker.record_failure(_msg_val(m))
                        sync_state()
                        actions.down([m])
                    else:
                        actions.down([m])

            unsub = src.subscribe(sink)
            sync_state()

            def cleanup() -> None:
                unsub()

            return cleanup

        out = node(
            start,
            describe_kind="operator",
            meta={"breaker_state": breaker.state},
            complete_when_deps_complete=False,
            initial=src.get(),
        )
        return WithBreakerBundle(node=out, breaker_state=out.meta["breaker_state"])

    return _op


@runtime_checkable
class TokenBucket(Protocol):
    """Protocol for token bucket instances (use :func:`token_bucket` to create)."""

    def available(self) -> float: ...
    def try_consume(self, cost: float = 1.0) -> bool: ...


class _TokenBucketImpl:
    """Thread-safe token bucket (for pairing with custom gates or metrics)."""

    __slots__ = ("_capacity", "_lock", "_refill_per_sec", "_tokens", "_updated_at")

    def __init__(self, capacity: float, refill_per_second: float) -> None:
        if capacity <= 0:
            msg = "capacity must be > 0"
            raise ValueError(msg)
        if refill_per_second < 0:
            msg = "refill_per_second must be >= 0"
            raise ValueError(msg)
        self._capacity = float(capacity)
        self._refill_per_sec = float(refill_per_second)
        self._tokens = float(capacity)
        self._updated_at = time.monotonic()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        if self._refill_per_sec > 0:
            elapsed = now - self._updated_at
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_sec)
        self._updated_at = now

    def available(self) -> float:
        with self._lock:
            self._refill_locked()
            return self._tokens

    def try_consume(self, cost: float = 1.0) -> bool:
        if cost <= 0:
            return True
        with self._lock:
            self._refill_locked()
            if self._tokens >= cost:
                self._tokens -= cost
                return True
            return False


def token_bucket(capacity: float, refill_per_second: float) -> TokenBucket:
    """Factory for a thread-safe :class:`TokenBucket`.

    Args:
        capacity: Maximum tokens.
        refill_per_second: Tokens restored per second (``0`` = no refill).
    """
    return _TokenBucketImpl(capacity, refill_per_second)


def token_tracker(capacity: float, refill_per_second: float) -> TokenBucket:
    """Alias for :func:`token_bucket` (backward compat)."""
    return token_bucket(capacity, refill_per_second)


def rate_limiter(source: Node[Any], max_events: int, window_seconds: float) -> Node[Any]:
    """Sliding-window limit: at most *max_events* ``DATA`` emissions per *window_seconds*.

    Values that exceed the window budget are queued (FIFO) and emitted as slots free.
    ``DIRTY`` / ``RESOLVED`` still track the primary when not blocked.
    """
    if max_events <= 0:
        msg = "max_events must be > 0"
        raise ValueError(msg)
    if window_seconds <= 0:
        msg = "window_seconds must be > 0"
        raise ValueError(msg)

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        times: deque[float] = deque()
        pending: deque[Any] = deque()
        timer: list[threading.Timer | None] = [None]
        timer_gen = [0]

        def cancel_timer() -> None:
            if timer[0] is not None:
                timer[0].cancel()
                timer[0] = None

        def prune(now: float) -> None:
            boundary = now - window_seconds
            while times and times[0] <= boundary:
                times.popleft()

        def schedule_emit(when: float) -> None:
            cancel_timer()
            timer_gen[0] += 1
            gen = timer_gen[0]
            delay = max(0.0, when - time.monotonic())

            def fire() -> None:
                if gen != timer_gen[0]:
                    return
                timer[0] = None
                try_emit()

            tt = threading.Timer(delay, fire)
            tt.daemon = True
            tt.start()
            timer[0] = tt

        def try_emit() -> None:
            while pending:
                now = time.monotonic()
                prune(now)
                if len(times) < max_events:
                    times.append(now)
                    actions.emit(pending.popleft())
                else:
                    oldest = times[0]
                    schedule_emit(oldest + window_seconds)
                    return

        def sink(msgs: Messages) -> None:
            for m in msgs:
                t = m[0]
                if t is MessageType.DIRTY:
                    actions.down([(MessageType.DIRTY,)])
                elif t is MessageType.DATA:
                    pending.append(_msg_val(m))
                    try_emit()
                elif t is MessageType.RESOLVED:
                    actions.down([(MessageType.RESOLVED,)])
                elif t is MessageType.COMPLETE:
                    cancel_timer()
                    pending.clear()
                    times.clear()
                    actions.down([(MessageType.COMPLETE,)])
                elif t is MessageType.ERROR:
                    cancel_timer()
                    pending.clear()
                    times.clear()
                    actions.down([m])
                else:
                    actions.down([m])

        unsub = source.subscribe(sink)

        def cleanup() -> None:
            timer_gen[0] += 1
            cancel_timer()
            unsub()

        return cleanup

    return node(
        start,
        describe_kind="operator",
        complete_when_deps_complete=False,
        initial=source.get(),
    )


StatusValue = Literal["pending", "active", "completed", "errored"]


@dataclass(frozen=True, slots=True)
class WithStatusBundle:
    """Result of :func:`with_status`: pass-through value plus companion nodes."""

    node: Node[Any]
    status: Node[StatusValue]
    error: Node[Any]


def with_status(
    src: Node[Any],
    *,
    initial_status: StatusValue = "pending",
) -> WithStatusBundle:
    """Mirror *src* with ``status`` and ``error`` companion state nodes.

    ``status`` moves ``pending`` → ``active`` on ``DATA``, ``completed`` on ``COMPLETE``, and
    ``errored`` on ``ERROR`` (``error`` holds the exception). After ``errored``, the next
    ``DATA`` clears ``error`` and sets ``active`` inside :func:`~graphrefly.core.protocol.batch`.

    Companions are wired into ``node.meta`` so they appear in ``describe()``.
    """

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        out.meta["status"].down([(MessageType.DATA, initial_status)])
        out.meta["error"].down([(MessageType.DATA, None)])

        def sink(msgs: Messages) -> None:
            for m in msgs:
                t = m[0]
                if t is MessageType.DIRTY:
                    actions.down([(MessageType.DIRTY,)])
                elif t is MessageType.DATA:
                    if out.meta["status"].get() == "errored":
                        with batch():
                            out.meta["error"].down([(MessageType.DATA, None)])
                            out.meta["status"].down([(MessageType.DATA, "active")])
                    else:
                        out.meta["status"].down([(MessageType.DATA, "active")])
                    actions.emit(_msg_val(m))
                elif t is MessageType.RESOLVED:
                    actions.down([(MessageType.RESOLVED,)])
                elif t is MessageType.COMPLETE:
                    out.meta["status"].down([(MessageType.DATA, "completed")])
                    actions.down([(MessageType.COMPLETE,)])
                elif t is MessageType.ERROR:
                    err = _msg_val(m)
                    with batch():
                        out.meta["error"].down([(MessageType.DATA, err)])
                        out.meta["status"].down([(MessageType.DATA, "errored")])
                    actions.down([m])
                else:
                    actions.down([m])

        unsub = src.subscribe(sink)

        def cleanup() -> None:
            unsub()

        return cleanup

    out = node(
        start,
        describe_kind="operator",
        meta={"status": initial_status, "error": None},
        complete_when_deps_complete=False,
        resubscribable=True,
        initial=src.get(),
    )
    return WithStatusBundle(node=out, status=out.meta["status"], error=out.meta["error"])
