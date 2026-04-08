"""AsyncioRunner — schedule coroutines on an asyncio event loop (roadmap §5.1)."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine


class AsyncioRunner:
    """Runner backed by a running :mod:`asyncio` event loop.

    Schedule coroutines via ``loop.create_task`` with thread-safe dispatch.
    Use inside an ``async def`` context (e.g. FastAPI lifespan, async test).

    Example::

        import asyncio
        from graphrefly.compat import AsyncioRunner
        from graphrefly.core.runner import set_default_runner

        async def main():
            runner = AsyncioRunner.from_running()
            set_default_runner(runner)
            # ... build graph, use from_awaitable, etc.

        asyncio.run(main())
    """

    __slots__ = ("_loop", "_scheduled", "_completed")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._scheduled = 0
        self._completed = 0

    @classmethod
    def from_running(cls) -> AsyncioRunner:
        """Create from the currently running asyncio event loop.

        Raises:
            RuntimeError: If no event loop is running.
        """
        return cls(asyncio.get_running_loop())

    def schedule(
        self,
        coro: Coroutine[Any, Any, Any],
        on_result: Callable[[Any], None],
        on_error: Callable[[BaseException], None],
    ) -> Callable[[], None]:
        task: asyncio.Task[Any] | None = None
        cancelled = threading.Event()

        def _create_task() -> None:
            nonlocal task
            if cancelled.is_set():
                self._completed += 1
                coro.close()
                return

            async def _wrapper() -> None:
                try:
                    result = await coro
                except asyncio.CancelledError:
                    raise
                except KeyboardInterrupt:
                    raise
                except SystemExit:
                    raise
                except BaseException as err:
                    on_error(err)
                else:
                    on_result(result)
                finally:
                    self._completed += 1

            task = self._loop.create_task(_wrapper())

        # Increment eagerly on the calling thread so __repr__ is always consistent.
        self._scheduled += 1
        try:
            self._loop.call_soon_threadsafe(_create_task)
        except RuntimeError:
            # Loop closed — cannot schedule; close the coroutine and balance the counter.
            self._completed += 1
            coro.close()

        def cancel() -> None:
            cancelled.set()
            if task is not None:
                task.cancel()

        return cancel

    def __repr__(self) -> str:
        pending = self._scheduled - self._completed
        running = self._loop.is_running()
        return (
            f"AsyncioRunner(scheduled={self._scheduled}, completed={self._completed}, "
            f"pending={pending}, loop_running={running})"
        )


__all__ = ["AsyncioRunner"]
