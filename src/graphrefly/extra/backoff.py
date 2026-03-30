"""Backoff strategies for :func:`~graphrefly.extra.resilience.retry` and circuit tooling."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Literal

type JitterMode = Literal["none", "full", "equal"]
type BackoffPreset = Literal[
    "constant", "linear", "exponential", "fibonacci", "decorrelated_jitter"
]
type BackoffStrategy = Callable[[int, BaseException | None, float | None], float | None]

__all__ = [
    "BackoffPreset",
    "BackoffStrategy",
    "JitterMode",
    "constant",
    "decorrelated_jitter",
    "exponential",
    "fibonacci",
    "linear",
    "resolve_backoff_preset",
    "with_max_attempts",
]


def _clamp_non_negative(value: float) -> float:
    return 0.0 if value < 0 else value


def _apply_jitter(delay: float, jitter: JitterMode) -> float:
    if jitter == "none":
        return delay
    if jitter == "full":
        return random.uniform(0.0, delay)
    return delay / 2.0 + random.uniform(0.0, delay / 2.0)


def constant(delay: float) -> BackoffStrategy:
    """Create a backoff strategy that always returns the same delay.

    Args:
        delay: Fixed delay in seconds (clamped to 0 if negative).

    Returns:
        A :data:`BackoffStrategy` callable.

    Example:
        ```python
        from graphrefly.extra.backoff import constant
        s = constant(2.0)
        assert s(0, None, None) == 2.0
        assert s(5, None, None) == 2.0
        ```
    """
    safe = _clamp_non_negative(delay)

    def _strategy(
        _attempt: int,
        _error: BaseException | None = None,
        _prev_delay: float | None = None,
    ) -> float:
        return safe

    return _strategy


def linear(base: float, step: float | None = None) -> BackoffStrategy:
    """Create a backoff strategy with linearly increasing delay.

    Delay is ``base + step * attempt`` where *step* defaults to *base*.

    Args:
        base: Starting delay in seconds.
        step: Increment per attempt (defaults to *base*).

    Returns:
        A :data:`BackoffStrategy` callable.

    Example:
        ```python
        from graphrefly.extra.backoff import linear
        s = linear(1.0)
        assert s(0, None, None) == 1.0
        assert s(2, None, None) == 3.0
        ```
    """
    safe_base = _clamp_non_negative(base)
    safe_step = safe_base if step is None else _clamp_non_negative(step)

    def _strategy(
        attempt: int,
        _error: BaseException | None = None,
        _prev_delay: float | None = None,
    ) -> float:
        return safe_base + safe_step * max(0, attempt)

    return _strategy


def exponential(
    *,
    base: float = 0.1,
    factor: float = 2.0,
    max_delay: float = 30.0,
    jitter: JitterMode = "none",
) -> BackoffStrategy:
    """Create an exponential backoff strategy capped at ``max_delay``.

    Args:
        base: Initial delay in seconds (default ``0.1``).
        factor: Multiplicative growth factor >= 1.0 (default ``2.0``).
        max_delay: Upper bound on delay in seconds (default ``30.0``).
        jitter: Jitter mode: ``"none"`` (default), ``"full"``, or ``"equal"``.

    Returns:
        A :data:`BackoffStrategy` callable.

    Example:
        ```python
        from graphrefly.extra.backoff import exponential
        s = exponential(base=0.1, factor=2.0, max_delay=30.0)
        assert s(0, None, None) == 0.1
        assert s(1, None, None) == 0.2
        ```
    """
    safe_base = _clamp_non_negative(base)
    safe_factor = 1.0 if factor < 1.0 else factor
    safe_max = _clamp_non_negative(max_delay)

    def _strategy(
        attempt: int,
        _error: BaseException | None = None,
        _prev_delay: float | None = None,
    ) -> float:
        if safe_base == 0.0:
            delay = 0.0
        elif safe_factor == 1.0:
            delay = safe_base
        else:
            cap_ratio = safe_max / safe_base if safe_base > 0 else 0.0
            raw_attempt = max(0, attempt)
            growth = 1.0
            for _ in range(raw_attempt):
                if growth >= cap_ratio:
                    growth = cap_ratio
                    break
                growth *= safe_factor
            delay = safe_base * growth
            if delay > safe_max:
                delay = safe_max
        return _apply_jitter(delay, jitter)

    return _strategy


def fibonacci(base: float = 0.1, *, max_delay: float = 30.0) -> BackoffStrategy:
    """Create a backoff strategy with Fibonacci-scaled delays.

    Delays follow the sequence ``1, 2, 3, 5, 8, … × base``, capped at
    ``max_delay``.

    Args:
        base: Multiplier in seconds (default ``0.1``).
        max_delay: Upper bound in seconds (default ``30.0``).

    Returns:
        A :data:`BackoffStrategy` callable.

    Example:
        ```python
        from graphrefly.extra.backoff import fibonacci
        s = fibonacci(1.0)
        assert s(0, None, None) == 1.0  # 1 × 1.0
        assert s(1, None, None) == 2.0  # 2 × 1.0
        ```
    """
    safe_base = _clamp_non_negative(base)
    safe_max = _clamp_non_negative(max_delay)

    def _fib_unit(attempt: int) -> float:
        if attempt <= 0:
            return 1.0
        prev, cur = 1.0, 2.0
        for _ in range(1, attempt):
            prev, cur = cur, prev + cur
        return cur

    def _strategy(
        attempt: int,
        _error: BaseException | None = None,
        _prev_delay: float | None = None,
    ) -> float:
        raw = _fib_unit(attempt) * safe_base
        return raw if raw <= safe_max else safe_max

    return _strategy


def decorrelated_jitter(
    base: float = 0.1,
    max_delay: float = 30.0,
) -> BackoffStrategy:
    """Decorrelated jitter (AWS-recommended): ``random(base, min(max, prev * 3))``.

    Stateless — uses ``prev_delay`` (passed by the consumer) instead of closure state.
    Safe to share across concurrent retry sequences.

    Args:
        base: Floor of the random range (seconds, default ``0.1``).
        max_delay: Ceiling cap (seconds, default ``30.0``).
    """
    safe_base = _clamp_non_negative(base)
    safe_max = _clamp_non_negative(max_delay)

    def _strategy(
        _attempt: int,
        _error: BaseException | None = None,
        prev_delay: float | None = None,
    ) -> float:
        last = prev_delay if prev_delay is not None else safe_base
        ceiling = min(safe_max, last * 3)
        return random.uniform(safe_base, ceiling)

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
        prev_delay: float | None = None,
    ) -> float | None:
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
        A :data:`BackoffStrategy` configured with default parameters.

    Example:
        ```python
        from graphrefly.extra.backoff import resolve_backoff_preset
        s = resolve_backoff_preset("exponential")
        assert s(0, None, None) == 0.1
        ```
    """
    if name == "constant":
        return constant(1.0)
    if name == "linear":
        return linear(1.0)
    if name == "exponential":
        return exponential()
    if name == "fibonacci":
        return fibonacci()
    if name == "decorrelated_jitter":
        return decorrelated_jitter()
    msg = f"Unknown backoff preset: {name!r}"
    raise ValueError(msg)
