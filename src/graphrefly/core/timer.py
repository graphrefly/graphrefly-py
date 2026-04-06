"""Resettable deadline timer — centralised primitive for timeout, retry,
and rate_limiter (§5.10 exception: these need cancel/restart;
from_timer creates a new Node per reset)."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


class ResettableTimer:
    """Thread-safe resettable deadline timer."""

    __slots__ = ("_timer", "_lock")

    def __init__(self) -> None:
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def start(self, delay_seconds: float, callback: Callable[[], Any]) -> None:
        """Schedule callback after delay_seconds. Cancels any pending timer."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(delay_seconds, callback)
            self._timer.daemon = True
            self._timer.start()

    def cancel(self) -> None:
        """Cancel the pending timer (if any)."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    @property
    def pending(self) -> bool:
        """Whether a timer is currently pending."""
        with self._lock:
            return self._timer is not None
