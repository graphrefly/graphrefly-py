"""Backoff strategies for :func:`~graphrefly.extra.resilience.retry` and circuit tooling."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Literal

type JitterMode = Literal["none", "full", "equal"]
type BackoffPreset = Literal["constant", "linear", "exponential", "fibonacci"]
type BackoffStrategy = Callable[[int, BaseException | None, float | None], float | None]

__all__ = [
    "BackoffPreset",
    "BackoffStrategy",
    "JitterMode",
    "constant",
    "exponential",
    "fibonacci",
    "linear",
    "resolve_backoff_preset",
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
    """Always yield the same delay (seconds)."""
    safe = _clamp_non_negative(delay)

    def _strategy(
        _attempt: int,
        _error: BaseException | None = None,
        _prev_delay: float | None = None,
    ) -> float:
        return safe

    return _strategy


def linear(base: float, step: float | None = None) -> BackoffStrategy:
    """Linear delay: ``base + step * attempt`` (``step`` defaults to ``base``)."""
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
    """Exponential delay capped by ``max_delay`` (optional jitter)."""
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
    """Fibonacci-scaled delay: ``1, 2, 3, 5, … × base`` per attempt, capped at ``max_delay``."""
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


def resolve_backoff_preset(name: BackoffPreset) -> BackoffStrategy:
    """Resolve a preset name to a strategy with default parameters."""
    if name == "constant":
        return constant(1.0)
    if name == "linear":
        return linear(1.0)
    if name == "exponential":
        return exponential()
    if name == "fibonacci":
        return fibonacci()
    msg = f"Unknown backoff preset: {name!r}"
    raise ValueError(msg)
