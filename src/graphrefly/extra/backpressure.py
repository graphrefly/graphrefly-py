"""Watermark-based backpressure controller — reactive PAUSE/RESUME flow control.

Purely synchronous, event-driven. No timers, no polling, no async.
Each controller instance uses a unique lock_id so multiple controllers
on the same upstream node do not collide.
"""

from __future__ import annotations

from typing import Any, NamedTuple, Protocol

from graphrefly.core.protocol import MessageType


class WatermarkOptions(NamedTuple):
    """Thresholds for watermark-based backpressure."""

    high_water_mark: int
    low_water_mark: int


class WatermarkController(Protocol):
    """Watermark-based backpressure controller interface."""

    @property
    def pending(self) -> int: ...
    @property
    def paused(self) -> bool: ...
    def on_enqueue(self) -> bool: ...
    def on_dequeue(self) -> bool: ...
    def dispose(self) -> None: ...


class _WatermarkControllerImpl:
    """Internal implementation — use :func:`create_watermark_controller`."""

    __slots__ = ("_send_up", "_high", "_low", "_lock_id", "_pending", "_paused")

    def __init__(
        self,
        send_up: Any,
        opts: WatermarkOptions,
    ) -> None:
        self._send_up = send_up
        self._high = opts.high_water_mark
        self._low = opts.low_water_mark
        self._lock_id: object = object()  # unforgeable identity, like TS Symbol
        self._pending = 0
        self._paused = False

    @property
    def pending(self) -> int:
        """Number of un-consumed items."""
        return self._pending

    @property
    def paused(self) -> bool:
        """Whether upstream is currently paused by this controller."""
        return self._paused

    def on_enqueue(self) -> bool:
        """Call when a DATA message is buffered. Returns ``True`` if PAUSE was sent."""
        self._pending += 1
        if not self._paused and self._pending >= self._high:
            self._paused = True
            self._send_up([(MessageType.PAUSE, self._lock_id)])
            return True
        return False

    def on_dequeue(self) -> bool:
        """Call when a buffered item is consumed. Returns ``True`` if RESUME was sent."""
        if self._pending > 0:
            self._pending -= 1
        if self._paused and self._pending <= self._low:
            self._paused = False
            self._send_up([(MessageType.RESUME, self._lock_id)])
            return True
        return False

    def dispose(self) -> None:
        """If paused, send RESUME to unblock upstream."""
        if self._paused:
            self._paused = False
            self._send_up([(MessageType.RESUME, self._lock_id)])


def create_watermark_controller(
    send_up: Any,
    opts: WatermarkOptions,
) -> WatermarkController:
    """Create a watermark-based backpressure controller.

    Purely synchronous, event-driven. No timers, no polling, no async.
    Each controller instance uses a unique ``lock_id`` (``object()``) so
    multiple controllers on the same upstream node do not collide.

    Args:
        send_up: Callback that delivers messages upstream (e.g. ``handle.up``).
        opts: High/low watermark thresholds (item counts).

    Returns:
        A :class:`WatermarkController`.

    Raises:
        ValueError: If watermark options are invalid.
    """
    if opts.high_water_mark < 1:
        raise ValueError("high_water_mark must be >= 1")
    if opts.low_water_mark < 0:
        raise ValueError("low_water_mark must be >= 0")
    if opts.low_water_mark >= opts.high_water_mark:
        raise ValueError("low_water_mark must be < high_water_mark")
    return _WatermarkControllerImpl(send_up, opts)
