"""Runner protocol — async event loop integration for GraphReFly (roadmap §5.1).

A :class:`Runner` bridges GraphReFly's synchronous reactive core to an async
event loop (asyncio, trio, etc.).  Sources like :func:`~graphrefly.extra.sources.from_awaitable`
and :func:`~graphrefly.extra.sources.from_async_iter` use the runner to schedule
coroutines instead of spawning daemon threads.

Usage::

    from graphrefly.core.runner import set_default_runner
    from graphrefly.compat import AsyncioRunner

    runner = AsyncioRunner.from_running()
    set_default_runner(runner)
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

# ---------------------------------------------------------------------------
# Runner protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Runner(Protocol):
    """Bridge between GraphReFly's sync core and an async event loop.

    Implementations must be safe to call ``schedule()`` from any thread.
    ``on_result`` / ``on_error`` may be invoked from the event loop thread
    (which may differ from the calling thread).
    """

    def schedule(
        self,
        coro: Coroutine[Any, Any, Any],
        on_result: Callable[[Any], None],
        on_error: Callable[[BaseException], None],
    ) -> Callable[[], None]:
        """Schedule *coro* for execution; return a cancel function.

        Args:
            coro: The coroutine to run.
            on_result: Called with the coroutine's return value on success.
            on_error: Called with the exception on failure.

        Returns:
            A callable that cancels the scheduled coroutine (best-effort).
        """
        ...


# ---------------------------------------------------------------------------
# Default runner — thread-local
# ---------------------------------------------------------------------------

_runner_tls = threading.local()


def set_default_runner(runner: Runner | None) -> None:
    """Set the default :class:`Runner` for the current thread.

    Pass ``None`` to clear the runner (subsequent calls to
    :func:`get_default_runner` will raise).
    """
    _runner_tls.runner = runner


def get_default_runner() -> Runner:
    """Return the current thread's default :class:`Runner`.

    Raises:
        RuntimeError: If no runner has been configured via
            :func:`set_default_runner`.
    """
    try:
        r = _runner_tls.runner
    except AttributeError:
        r = None
    if r is None:
        msg = (
            "No Runner configured. Call set_default_runner() with an "
            "AsyncioRunner or TrioRunner before using async sources. "
            "Example: set_default_runner(AsyncioRunner.from_running())"
        )
        raise RuntimeError(msg)
    return r  # type: ignore[no-any-return]


def resolve_runner(runner: Runner | None) -> Runner:
    """Return *runner* if provided, otherwise :func:`get_default_runner`."""
    if runner is not None:
        return runner
    return get_default_runner()


def is_runner_registered() -> bool:
    """Return ``True`` if a default :class:`Runner` is set for the current thread.

    **Debug / test only** — not part of the public API. Used by
    ``harnessProfile`` to surface ``runner_registered=False`` in diagnostic
    output so missing-Runner stalls are immediately visible.
    """
    try:
        r = _runner_tls.runner
    except AttributeError:
        return False
    return r is not None


__all__ = [
    "Runner",
    "get_default_runner",
    "is_runner_registered",
    "resolve_runner",
    "set_default_runner",
]
