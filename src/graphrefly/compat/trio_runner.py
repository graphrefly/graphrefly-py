"""TrioRunner — schedule coroutines on a trio nursery (roadmap §5.1).

Requires the ``trio`` package (optional dependency).

Usage::

    import trio
    from graphrefly.compat.trio_runner import TrioRunner
    from graphrefly.core.runner import set_default_runner

    async def main():
        async with trio.open_nursery() as nursery:
            runner = TrioRunner(nursery)
            set_default_runner(runner)
            # ... build graph, use from_awaitable, etc.

    trio.run(main)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    import trio


class TrioRunner:
    """Runner backed by a :mod:`trio` nursery.

    Each scheduled coroutine runs as a trio task in the nursery.
    Cancel scopes provide best-effort cancellation.
    """

    __slots__ = ("_nursery",)

    def __init__(self, nursery: trio.Nursery) -> None:
        self._nursery = nursery

    def schedule(
        self,
        coro: Coroutine[Any, Any, Any],
        on_result: Callable[[Any], None],
        on_error: Callable[[BaseException], None],
    ) -> Callable[[], None]:
        import trio as _trio

        cancel_scope = _trio.CancelScope()
        cancelled = False

        async def _wrapper() -> None:
            with cancel_scope:
                if cancelled:
                    coro.close()
                    return
                try:
                    result = await coro
                except _trio.Cancelled:
                    raise
                except KeyboardInterrupt:
                    raise
                except SystemExit:
                    raise
                except BaseException as err:
                    on_error(err)
                else:
                    on_result(result)

        self._nursery.start_soon(_wrapper)

        def cancel() -> None:
            nonlocal cancelled
            cancelled = True
            cancel_scope.cancel()

        return cancel


__all__ = ["TrioRunner"]
