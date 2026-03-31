"""Backoff strategies for :func:`~graphrefly.extra.resilience.retry` and circuit tooling."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Literal

type JitterMode = Literal["none", "full", "equal"]
type BackoffPreset = Literal[
    "constant", "linear", "exponential", "fibonacci", "decorrelated_jitter"
]
type BackoffStrategy = Callable[[int, BaseException | None, int | None], int | None]

NS_PER_MS = 1_000_000
NS_PER_SEC = 1_000_000_000

__all__ = [
    "BackoffPreset",
    "BackoffStrategy",
    "JitterMode",
    "NS_PER_MS",
    "NS_PER_SEC",
    "constant",
    "decorrelated_jitter",
    "exponential",
    "fibonacci",
    "linear",
    "resolve_backoff_preset",
    "with_max_attempts",
]


def _clamp_non_negative(value: int) -> int:
    return 0 if value < 0 else value


def _apply_jitter(delay: int, jitter: JitterMode) -> int:
    if jitter == "none":
        return delay
    if jitter == "full":
        return random.randint(0, delay) if delay > 0 else 0
    # "equal" — half deterministic, half random
    half = delay // 2
    return half + random.randint(0, delay - half) if delay > 0 else 0


def constant(delay_ns: int) -> BackoffStrategy:
    """Create a backoff strategy that always returns the same delay.

    Args:
        delay_ns: Fixed delay in nanoseconds (clamped to 0 if negative).

    Returns:
        A :data:`BackoffStrategy` callable.

    Example:
        ```python
        from graphrefly.extra.backoff import constant, NS_PER_SEC
        s = constant(2 * NS_PER_SEC)
        assert s(0, None, None) == 2_000_000_000
        assert s(5, None, None) == 2_000_000_000
        ```
    """
    safe = _clamp_non_negative(delay_ns)

    def _strategy(
        _attempt: int,
        _error: BaseException | None = None,
        _prev_delay: int | None = None,
    ) -> int:
        return safe

    return _strategy


def linear(base_ns: int, step_ns: int | None = None) -> BackoffStrategy:
    """Create a backoff strategy with linearly increasing delay.

    Delay is ``base_ns + step_ns * attempt`` where *step_ns* defaults to *base_ns*.

    Args:
        base_ns: Starting delay in nanoseconds.
        step_ns: Increment per attempt in nanoseconds (defaults to *base_ns*).

    Returns:
        A :data:`BackoffStrategy` callable.

    Example:
        ```python
        from graphrefly.extra.backoff import linear, NS_PER_SEC
        s = linear(1 * NS_PER_SEC)
        assert s(0, None, None) == 1_000_000_000
        assert s(2, None, None) == 3_000_000_000
        ```
    """
    safe_base = _clamp_non_negative(base_ns)
    safe_step = safe_base if step_ns is None else _clamp_non_negative(step_ns)

    def _strategy(
        attempt: int,
        _error: BaseException | None = None,
        _prev_delay: int | None = None,
    ) -> int:
        return safe_base + safe_step * max(0, attempt)

    return _strategy


def exponential(
    *,
    base_ns: int = 100_000_000,
    factor: float = 2.0,
    max_delay_ns: int = 30_000_000_000,
    jitter: JitterMode = "none",
) -> BackoffStrategy:
    """Create an exponential backoff strategy capped at ``max_delay_ns``.

    Args:
        base_ns: Initial delay in nanoseconds (default ``100_000_000`` = 100 ms).
        factor: Multiplicative growth factor >= 1.0 (default ``2.0``).
        max_delay_ns: Upper bound on delay in nanoseconds (default ``30_000_000_000`` = 30 s).
        jitter: Jitter mode: ``"none"`` (default), ``"full"``, or ``"equal"``.

    Returns:
        A :data:`BackoffStrategy` callable.

    Example:
        ```python
        from graphrefly.extra.backoff import exponential
        s = exponential(base_ns=100_000_000, factor=2.0, max_delay_ns=30_000_000_000)
        assert s(0, None, None) == 100_000_000
        assert s(1, None, None) == 200_000_000
        ```
    """
    safe_base = _clamp_non_negative(base_ns)
    safe_factor = 1.0 if factor < 1.0 else factor
    safe_max = _clamp_non_negative(max_delay_ns)

    def _strategy(
        attempt: int,
        _error: BaseException | None = None,
        _prev_delay: int | None = None,
    ) -> int:
        if safe_base == 0:
            return _apply_jitter(0, jitter)
        if safe_factor == 1.0:
            return _apply_jitter(safe_base, jitter)

        cap_ratio = safe_max / safe_base if safe_base > 0 else 0.0
        raw_attempt = max(0, attempt)
        growth = 1.0
        for _ in range(raw_attempt):
            if growth >= cap_ratio:
                growth = cap_ratio
                break
            growth *= safe_factor
        delay = int(safe_base * growth)
        if delay > safe_max:
            delay = safe_max
        return _apply_jitter(delay, jitter)

    return _strategy


def fibonacci(base_ns: int = 100_000_000, *, max_delay_ns: int = 30_000_000_000) -> BackoffStrategy:
    """Create a backoff strategy with Fibonacci-scaled delays.

    Delays follow the sequence ``1, 2, 3, 5, 8, ... * base_ns``, capped at
    ``max_delay_ns``.

    Args:
        base_ns: Multiplier in nanoseconds (default ``100_000_000`` = 100 ms).
        max_delay_ns: Upper bound in nanoseconds (default ``30_000_000_000`` = 30 s).

    Returns:
        A :data:`BackoffStrategy` callable.

    Example:
        ```python
        from graphrefly.extra.backoff import fibonacci, NS_PER_SEC
        s = fibonacci(1 * NS_PER_SEC)
        assert s(0, None, None) == 1_000_000_000  # 1 * 1s
        assert s(1, None, None) == 2_000_000_000  # 2 * 1s
        ```
    """
    safe_base = _clamp_non_negative(base_ns)
    safe_max = _clamp_non_negative(max_delay_ns)

    def _fib_unit(attempt: int) -> int:
        if attempt <= 0:
            return 1
        prev, cur = 1, 2
        for _ in range(1, attempt):
            prev, cur = cur, prev + cur
        return cur

    def _strategy(
        attempt: int,
        _error: BaseException | None = None,
        _prev_delay: int | None = None,
    ) -> int:
        raw = _fib_unit(attempt) * safe_base
        return raw if raw <= safe_max else safe_max

    return _strategy


def decorrelated_jitter(
    base_ns: int = 100_000_000,
    max_delay_ns: int = 30_000_000_000,
) -> BackoffStrategy:
    """Decorrelated jitter (AWS-recommended): ``random(base_ns, min(max, prev * 3))``.

    Stateless — uses ``prev_delay`` (passed by the consumer) instead of closure state.
    Safe to share across concurrent retry sequences.

    Args:
        base_ns: Floor of the random range (nanoseconds, default ``100_000_000`` = 100 ms).
        max_delay_ns: Ceiling cap (nanoseconds, default ``30_000_000_000`` = 30 s).
    """
    safe_base = _clamp_non_negative(base_ns)
    safe_max = _clamp_non_negative(max_delay_ns)

    def _strategy(
        _attempt: int,
        _error: BaseException | None = None,
        prev_delay: int | None = None,
    ) -> int:
        last = prev_delay if prev_delay is not None else safe_base
        ceiling = min(safe_max, last * 3)
        if ceiling <= safe_base:
            return safe_base
        return random.randint(safe_base, ceiling)

    return _strategy


def with_max_attempts(strategy: BackoffStrategy, max_attempts: int) -> BackoffStrategy:
    """Cap any strategy at *max_attempts*; returns ``None`` after the cap.

    Args:
        strategy: Inner strategy to wrap.
        max_attempts: Maximum number of attempts (inclusive).
    """

    def _strategy(
        attempt: int,
        error: BaseException | None = None,
        prev_delay: int | None = None,
    ) -> int | None:
        if attempt >= max_attempts:
            return None
        return strategy(attempt, error, prev_delay)

    return _strategy


def resolve_backoff_preset(name: BackoffPreset) -> BackoffStrategy:
    """Resolve a preset name string to a :data:`BackoffStrategy` with default parameters.

    Args:
        name: One of ``"constant"``, ``"linear"``, ``"exponential"``,
            ``"fibonacci"``, or ``"decorrelated_jitter"``.

    Returns:
        A :data:`BackoffStrategy` configured with default nanosecond parameters.

    Example:
        ```python
        from graphrefly.extra.backoff import resolve_backoff_preset
        s = resolve_backoff_preset("exponential")
        assert s(0, None, None) == 100_000_000
        ```
    """
    if name == "constant":
        return constant(NS_PER_SEC)
    if name == "linear":
        return linear(NS_PER_SEC)
    if name == "exponential":
        return exponential()
    if name == "fibonacci":
        return fibonacci()
    if name == "decorrelated_jitter":
        return decorrelated_jitter()
    msg = f"Unknown backoff preset: {name!r}"
    raise ValueError(msg)
