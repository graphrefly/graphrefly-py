"""Cooperative cancellation token for async/LLM boundaries.

The ``CancellationToken`` protocol provides a small interface for cooperative
cancellation that mirrors TypeScript's ``AbortSignal`` pattern.  It is backed
by ``threading.Event`` and is fully reactive (no polling — callbacks fire
immediately on cancellation).

Usage::

    token = cancellation_token()
    token.on_cancel(lambda: print("cancelled"))
    token.cancel()  # fires callback synchronously

Pass into ``LLMAdapter.invoke()`` via ``LLMInvokeOptions`` so adapters can
react to cancellation without polling.
"""

from __future__ import annotations

import threading
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable


@runtime_checkable
class CancellationToken(Protocol):
    """Cooperative cancellation token (cross-language: TS ``AbortSignal``)."""

    @property
    def is_cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        ...

    def on_cancel(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Register a callback to fire on cancellation.

        Returns an unsubscribe callable that removes the callback.
        If already cancelled, ``fn`` fires synchronously before returning.
        """
        ...


class _CancellationTokenImpl:
    """Concrete cancellation token backed by ``threading.Event``."""

    __slots__ = ("_event", "_callbacks", "_lock")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def on_cancel(self, fn: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            if self._event.is_set():
                fn()
                return lambda: None
            self._callbacks.append(fn)

        def _unsub() -> None:
            with self._lock, suppress(ValueError):
                self._callbacks.remove(fn)

        return _unsub

    def cancel(self) -> None:
        """Request cancellation.  Fires all registered callbacks synchronously."""
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            cbs = list(self._callbacks)
            self._callbacks.clear()
        for cb in cbs:
            cb()


def cancellation_token() -> _CancellationTokenImpl:
    """Create a new cancellation token.

    Returns a concrete token with ``cancel()``, ``is_cancelled``, and
    ``on_cancel(fn)`` methods.
    """
    return _CancellationTokenImpl()
