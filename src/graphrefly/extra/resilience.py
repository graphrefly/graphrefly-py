"""Resilience utilities — roadmap §3.1 + §3.1c (retry, breaker, rate limit, status,
fallback, cache, timeout).
"""

from __future__ import annotations

import builtins
import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from graphrefly.core.clock import monotonic_ns
from graphrefly.core.node import Node, NodeActions, node
from graphrefly.core.protocol import Messages, MessageType, batch
from graphrefly.core.timer import ResettableTimer
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
    "TimeoutError",
    "TokenBucket",
    "WithBreakerBundle",
    "WithStatusBundle",
    "cache",
    "circuit_breaker",
    "fallback",
    "rate_limiter",
    "retry",
    "timeout",
    "token_bucket",
    "token_tracker",
    "with_breaker",
    "with_status",
]


def _msg_val(m: tuple[Any, ...]) -> Any:
    assert len(m) >= 2
    return m[1]


def _coerce_delay_ns(raw_delay: Any) -> int:
    if isinstance(raw_delay, bool) or not isinstance(raw_delay, int | float):
        msg = "backoff strategy must return an int/float nanosecond delay or None"
        raise TypeError(msg)
    if not math.isfinite(float(raw_delay)):
        msg = "backoff strategy delay must be finite"
        raise TypeError(msg)
    delay = int(raw_delay)
    return 0 if delay < 0 else delay


def retry(
    source: Node[Any],
    count: int | None = None,
    *,
    backoff: BackoffStrategy | BackoffPreset | None = None,
) -> Node[Any]:
    """Retry upstream after ``ERROR`` with optional backoff between attempts.

    Unsubscribes from the source after each terminal ``ERROR``, waits (possibly
    zero), then resubscribes. Successful ``DATA`` resets the attempt counter.
    For a useful retry, the source should use ``resubscribable=True`` so a new
    subscription can deliver again after a terminal error.

    Args:
        source: The upstream :class:`~graphrefly.core.node.Node` to retry.
        count: Maximum retry attempts (``None`` = unlimited when *backoff* is set,
            0 otherwise).
        backoff: Optional :data:`~graphrefly.extra.backoff.BackoffStrategy` or
            :data:`~graphrefly.extra.backoff.BackoffPreset` name for delay between
            retries.

    Returns:
        A new :class:`~graphrefly.core.node.Node` wrapping the retry logic.

    Example:
        ```python
        from graphrefly.extra.resilience import retry
        from graphrefly.extra import of
        n = retry(of(1), count=3)
        ```
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
        prev_delay: list[int | None] = [None]
        upstream_unsub: list[Callable[[], None] | None] = [None]
        retry_timer = ResettableTimer()
        timer_generation = [0]
        lock = threading.Lock()

        def disconnect_upstream() -> None:
            if upstream_unsub[0] is not None:
                upstream_unsub[0]()
                upstream_unsub[0] = None

        def connect() -> None:
            retry_timer.cancel()
            disconnect_upstream()

            def sink(msgs: Messages) -> None:
                for m in msgs:
                    if stopped[0]:
                        return
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

                raw = 0 if strategy is None else strategy(attempt[0], err, prev_delay[0])
                delay_ns = _coerce_delay_ns(0 if raw is None else raw)
                prev_delay[0] = delay_ns
                attempt[0] += 1
                timer_generation[0] += 1
                current_gen = timer_generation[0]
                disconnect_upstream()

            safe_delay = delay_ns / 1_000_000_000 if delay_ns > 0 else 1e-6

            def fire() -> None:
                with lock:
                    if stopped[0] or current_gen != timer_generation[0]:
                        return
                # connect() outside lock — subscribe may trigger immediate emission
                connect()

            # §5.10: ResettableTimer (not from_timer) — retry delay needs cancel/restart;
            # from_timer creates a new Node per reset, adding lifecycle overhead per retry.
            retry_timer.start(safe_delay, fire)

        connect()

        def cleanup() -> None:
            with lock:
                stopped[0] = True
                timer_generation[0] += 1
            retry_timer.cancel()
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
    """Raised when ``with_breaker(..., on_open="error")`` sees an open circuit.

    Example:
        ```python
        from graphrefly.extra.resilience import CircuitOpenError, circuit_breaker, with_breaker
        from graphrefly import state
        from graphrefly.extra.sources import first_value_from
        breaker = circuit_breaker(failure_threshold=1)
        breaker.record_failure()  # open the circuit
        src = state(1)
        bundle = with_breaker(breaker, on_open="error")(src)
        # next DATA from src will raise CircuitOpenError
        ```
    """

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
        cooldown_ns: int = 30_000_000_000,
        cooldown_strategy: BackoffStrategy | None = None,
        half_open_max: int = 1,
    ) -> None:
        self._threshold = max(1, failure_threshold)
        self._cooldown_base = max(0, cooldown_ns)
        self._cooldown_strategy = cooldown_strategy
        self._half_open_max = max(1, half_open_max)
        self._state: CircuitState = "closed"
        self._failures = 0
        self._opened_at = 0
        self._trials = 0
        self._open_cycle = 0
        self._last_cooldown = self._cooldown_base
        self._lock = threading.Lock()

    def _get_cooldown(self) -> int:
        if self._cooldown_strategy is None:
            return self._cooldown_base
        raw = self._cooldown_strategy(self._open_cycle, None, None)
        return int(raw) if raw is not None else self._cooldown_base

    def _transition_to_open(self) -> None:
        self._state = "open"
        self._last_cooldown = self._get_cooldown()
        self._opened_at = monotonic_ns()
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
                if (monotonic_ns() - self._opened_at) >= self._last_cooldown:
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
    cooldown_ns: int = 30_000_000_000,
    cooldown_strategy: BackoffStrategy | None = None,
    half_open_max: int = 1,
) -> CircuitBreaker:
    """Thread-safe circuit breaker (closed / open / half-open).

    Supports escalating cooldown via an optional backoff strategy.

    Args:
        failure_threshold: Failures before opening (default ``5``).
        cooldown_ns: Base cooldown in nanoseconds (default ``30_000_000_000`` = 30 s).
        cooldown_strategy: Backoff for cooldown escalation.
        half_open_max: Trials in half-open (default ``1``).
    """
    return _CircuitBreakerImpl(
        failure_threshold=failure_threshold,
        cooldown_ns=cooldown_ns,
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
    """Guard a source node with a :class:`CircuitBreaker`.

    On each upstream ``DATA``, if the breaker refuses work, either emit
    ``RESOLVED`` (``on_open="skip"``) or ``ERROR`` with :exc:`CircuitOpenError`
    (``on_open="error"``). ``COMPLETE`` records success; ``ERROR`` records
    failure and is forwarded. The ``breaker_state`` companion is wired into
    ``node.meta`` so it appears in ``describe()``.

    Args:
        breaker: A :class:`CircuitBreaker` instance (see :func:`circuit_breaker`).
        on_open: ``"skip"`` (emit ``RESOLVED``) or ``"error"`` (emit
            :exc:`CircuitOpenError`) when the circuit is open.

    Returns:
        A unary operator ``(Node) -> WithBreakerBundle``.

    Example:
        ```python
        from graphrefly import state
        from graphrefly.extra.resilience import circuit_breaker, with_breaker
        breaker = circuit_breaker(failure_threshold=3)
        src = state(1)
        bundle = with_breaker(breaker)(src)
        ```
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
        self._updated_at = monotonic_ns()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = monotonic_ns()
        if self._refill_per_sec > 0:
            elapsed_sec = (now - self._updated_at) / 1_000_000_000
            self._tokens = min(self._capacity, self._tokens + elapsed_sec * self._refill_per_sec)
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
    """Create a token bucket (alias for :func:`token_bucket`, naming parity with graphrefly-ts).

    Args:
        capacity: Maximum number of tokens.
        refill_per_second: Tokens restored per second (``0`` = no refill).

    Returns:
        A :class:`TokenBucket` instance.

    Example:
        ```python
        from graphrefly.extra.resilience import token_tracker
        tb = token_tracker(10, 2.0)
        assert tb.try_consume(1)
        ```
    """
    return token_bucket(capacity, refill_per_second)


def rate_limiter(source: Node[Any], max_events: int, window_ns: int) -> Node[Any]:
    """Limit upstream ``DATA`` to at most *max_events* per *window_ns* (sliding window).

    Values exceeding the budget are queued (FIFO) and emitted as slots free.
    ``DIRTY`` and ``RESOLVED`` still propagate immediately when not blocked.

    Args:
        source: The upstream :class:`~graphrefly.core.node.Node` to rate-limit.
        max_events: Maximum ``DATA`` emissions allowed per window.
        window_ns: Window duration in nanoseconds.

    Returns:
        A new :class:`~graphrefly.core.node.Node` with rate-limiting applied.

    Example:
        ```python
        from graphrefly import state
        from graphrefly.extra.resilience import rate_limiter
        from graphrefly.extra.sources import to_list
        from graphrefly.extra import of
        n = rate_limiter(of(1, 2, 3), max_events=2, window_ns=60_000_000_000)
        ```
    """
    if max_events <= 0:
        msg = "max_events must be > 0"
        raise ValueError(msg)
    if window_ns <= 0:
        msg = "window_ns must be > 0"
        raise ValueError(msg)

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        times: deque[int] = deque()
        pending: deque[Any] = deque()
        rl_timer = ResettableTimer()
        timer_gen = [0]

        def prune(now: int) -> None:
            boundary = now - window_ns
            while times and times[0] <= boundary:
                times.popleft()

        def schedule_emit(when_ns: int) -> None:
            timer_gen[0] += 1
            gen = timer_gen[0]
            delay = max(0.0, (when_ns - monotonic_ns()) / 1_000_000_000)

            def fire() -> None:
                if gen != timer_gen[0]:
                    return
                try_emit()

            # §5.10: ResettableTimer (not from_timer) — sliding-window
            # needs cancel/restart; from_timer adds Node overhead per check.
            rl_timer.start(delay, fire)

        def try_emit() -> None:
            while pending:
                now = monotonic_ns()
                prune(now)
                if len(times) < max_events:
                    times.append(now)
                    actions.emit(pending.popleft())
                else:
                    oldest = times[0]
                    schedule_emit(oldest + int(window_ns))
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
                    rl_timer.cancel()
                    pending.clear()
                    times.clear()
                    actions.down([(MessageType.COMPLETE,)])
                elif t is MessageType.ERROR:
                    rl_timer.cancel()
                    pending.clear()
                    times.clear()
                    actions.down([m])
                else:
                    actions.down([m])

        unsub = source.subscribe(sink)

        def cleanup() -> None:
            timer_gen[0] += 1
            rl_timer.cancel()
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

    ``status`` moves ``pending`` → ``active`` on ``DATA``, ``completed`` on
    ``COMPLETE``, and ``errored`` on ``ERROR`` (``error`` holds the exception).
    After ``errored``, the next ``DATA`` clears ``error`` and sets ``active``
    inside :func:`~graphrefly.core.protocol.batch`. Both companions are wired
    into ``node.meta`` so they appear in ``describe()``.

    Args:
        src: The upstream :class:`~graphrefly.core.node.Node` to track.
        initial_status: Initial value of the ``status`` companion (default
            ``"pending"``).

    Returns:
        A :class:`WithStatusBundle` with ``node``, ``status``, and ``error`` fields.

    Example:
        ```python
        from graphrefly import state
        from graphrefly.extra.resilience import with_status
        src = state(None)
        bundle = with_status(src)
        assert bundle.status.get() == "pending"
        ```
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


class TimeoutError(builtins.TimeoutError):  # noqa: A001
    """Raised when :func:`timeout` fires before upstream delivers ``DATA``.

    Subclasses the built-in :class:`builtins.TimeoutError` so ``except TimeoutError``
    catches both GraphReFly timeouts and stdlib timeouts.
    """

    def __init__(self, timeout_ns: int) -> None:
        super().__init__(f"Timed out after {timeout_ns / 1_000_000}ms")
        self.timeout_ns = timeout_ns


def fallback(source: Node[Any], fb: Any) -> Node[Any]:
    """On upstream terminal ``ERROR``, substitute *fb*.

    If *fb* is a plain value, emit ``[(DATA, fb), (COMPLETE,)]``.
    If *fb* is a :class:`~graphrefly.core.node.Node`, subscribe to it and
    forward its messages.

    All non-``ERROR`` messages pass through unchanged.

    Args:
        source: The upstream :class:`~graphrefly.core.node.Node`.
        fb: Fallback value or :class:`~graphrefly.core.node.Node`.

    Returns:
        A new :class:`~graphrefly.core.node.Node` with fallback logic.

    Example:
        ```python
        from graphrefly.extra.resilience import fallback
        from graphrefly.extra.sources import throw_error
        n = fallback(throw_error(ValueError("boom")), 42)
        ```
    """
    is_fb_node = isinstance(fb, Node)

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        fb_unsub: list[Callable[[], None] | None] = [None]

        unsub_holder: list[Callable[[], None] | None] = [None]

        def sink(msgs: Messages) -> None:
            for m in msgs:
                t = m[0]
                if t is MessageType.ERROR:
                    if unsub_holder[0] is not None:
                        unsub_holder[0]()  # release source subscription
                    if is_fb_node:

                        def fb_sink(fb_msgs: Messages) -> None:
                            actions.down(list(fb_msgs))

                        fb_unsub[0] = fb.subscribe(fb_sink)
                        cur = fb.get()
                        if cur is not None:
                            actions.down([(MessageType.DATA, cur)])
                    else:
                        actions.emit(fb)
                        actions.down([(MessageType.COMPLETE,)])
                    return
                if t is MessageType.TEARDOWN:
                    if fb_unsub[0] is not None:
                        fb_unsub[0]()
                    actions.down([m])
                    return
                if t is MessageType.DATA:
                    actions.emit(_msg_val(m))
                else:
                    actions.down([m])

        unsub_holder[0] = source.subscribe(sink)
        unsub: Callable[[], None] = unsub_holder[0]  # type: ignore[assignment]

        def cleanup() -> None:
            unsub()
            if fb_unsub[0] is not None:
                fb_unsub[0]()

        return cleanup

    return node(
        start,
        describe_kind="operator",
        complete_when_deps_complete=False,
        initial=source.get(),
    )


def timeout(source: Node[Any], timeout_ns: int) -> Node[Any]:
    """Emit ``ERROR(TimeoutError)`` if no ``DATA`` arrives within *timeout_ns*.

    Timer starts on subscription and resets on each ``DATA``.
    ``COMPLETE`` or ``ERROR`` from upstream cancel the timer.

    Args:
        source: The upstream :class:`~graphrefly.core.node.Node`.
        timeout_ns: Timeout duration in nanoseconds.

    Returns:
        A new :class:`~graphrefly.core.node.Node` with timeout logic.

    Example:
        ```python
        from graphrefly.extra.resilience import timeout
        from graphrefly.extra.sources import never
        n = timeout(never(), 50_000_000)
        ```
    """
    if timeout_ns <= 0:
        msg = "timeout_ns must be > 0"
        raise ValueError(msg)

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        to_timer = ResettableTimer()
        timer_gen = [0]
        done = [False]

        def arm_timer() -> None:
            timer_gen[0] += 1
            gen = timer_gen[0]
            delay_s = timeout_ns / 1_000_000_000

            def fire() -> None:
                if gen != timer_gen[0] or done[0]:
                    return
                done[0] = True
                # §5.10: ResettableTimer (not from_timer) — resettable
                # deadline; from_timer adds Node overhead per DATA reset.
                if unsub_holder[0] is not None:
                    unsub_holder[0]()
                actions.down([(MessageType.ERROR, TimeoutError(timeout_ns))])

            to_timer.start(delay_s, fire)

        unsub_holder: list[Callable[[], None] | None] = [None]

        def sink(msgs: Messages) -> None:
            for m in msgs:
                if done[0]:
                    return
                t = m[0]
                if t is MessageType.DATA:
                    arm_timer()
                    actions.emit(_msg_val(m))
                elif t is MessageType.COMPLETE:
                    to_timer.cancel()
                    done[0] = True
                    actions.down([(MessageType.COMPLETE,)])
                elif t is MessageType.ERROR:
                    to_timer.cancel()
                    done[0] = True
                    actions.down([m])
                elif t is MessageType.TEARDOWN:
                    to_timer.cancel()
                    done[0] = True
                    actions.down([m])
                    return
                elif t is MessageType.DIRTY:
                    actions.down([(MessageType.DIRTY,)])
                elif t is MessageType.RESOLVED:
                    actions.down([(MessageType.RESOLVED,)])
                else:
                    actions.down([m])

        arm_timer()
        unsub_holder[0] = source.subscribe(sink)
        unsub: Callable[[], None] = unsub_holder[0]  # type: ignore[assignment]

        def cleanup() -> None:
            done[0] = True
            timer_gen[0] += 1
            to_timer.cancel()
            unsub()

        return cleanup

    return node(
        start,
        describe_kind="operator",
        complete_when_deps_complete=False,
        initial=source.get(),
    )


def cache(source: Node[Any], ttl_ns: int) -> Node[Any]:
    """Memoize last ``DATA`` value with a TTL.

    On new subscriber, if a cached value exists within TTL, replay it
    immediately then forward live messages.

    Args:
        source: The upstream :class:`~graphrefly.core.node.Node`.
        ttl_ns: Time-to-live in nanoseconds for the cached value.

    Returns:
        A new :class:`~graphrefly.core.node.Node` with caching logic.

    Example:
        ```python
        from graphrefly.extra.resilience import cache
        from graphrefly.extra.sources import of
        n = cache(of(1), 60_000_000_000)
        ```
    """
    if ttl_ns <= 0:
        msg = "ttl_ns must be > 0"
        raise ValueError(msg)

    cached_value: list[Any] = [None]
    cached_at: list[int] = [0]
    has_cache = [False]

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        # Replay cached value if still within TTL
        if has_cache[0] and (monotonic_ns() - cached_at[0]) < ttl_ns:
            actions.down([(MessageType.DATA, cached_value[0])])

        def sink(msgs: Messages) -> None:
            for m in msgs:
                t = m[0]
                if t is MessageType.DATA:
                    val = _msg_val(m)
                    cached_value[0] = val
                    cached_at[0] = monotonic_ns()
                    has_cache[0] = True
                    actions.emit(val)
                elif t is MessageType.DIRTY:
                    actions.down([(MessageType.DIRTY,)])
                elif t is MessageType.RESOLVED:
                    actions.down([(MessageType.RESOLVED,)])
                elif t is MessageType.COMPLETE:
                    actions.down([(MessageType.COMPLETE,)])
                elif t is MessageType.ERROR:
                    actions.down([m])
                else:
                    actions.down([m])

        unsub = source.subscribe(sink)
        return unsub

    return node(
        start,
        describe_kind="operator",
        complete_when_deps_complete=False,
        resubscribable=True,
        initial=source.get(),
    )
