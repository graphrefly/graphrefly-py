"""Centralised timestamp utilities.

Convention: all graphrefly-py timestamps use nanoseconds (``_ns`` suffix).

- :func:`monotonic_ns` — monotonic clock (ordering, durations, timeline events).
- :func:`wall_clock_ns` — wall-clock (mutation attribution, cron emission).
"""

from time import monotonic_ns as _monotonic_ns
from time import time_ns as _time_ns


def monotonic_ns() -> int:
    """Monotonic nanosecond timestamp via :func:`time.monotonic_ns`."""
    return _monotonic_ns()


def wall_clock_ns() -> int:
    """Wall-clock nanosecond timestamp via :func:`time.time_ns`."""
    return _time_ns()
