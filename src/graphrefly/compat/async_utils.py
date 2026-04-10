"""Async utility functions for consuming GraphReFly nodes from async code (roadmap §5.1).

All utilities are **reactive** — they subscribe to nodes via
:meth:`~graphrefly.core.node.NodeImpl.subscribe` and bridge to the async world
via :class:`asyncio.Event` / :class:`asyncio.Queue`.  No polling.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from graphrefly.core.node import NO_VALUE
from graphrefly.core.protocol import MessageType

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from graphrefly.core.node import Node
    from graphrefly.core.protocol import Messages


async def to_async_iter(source: Node[Any]) -> AsyncIterator[Any]:
    """Yield values from *source* as an async iterator.

    Subscribes reactively — each ``DATA`` message payload is yielded, and
    each ``RESOLVED`` message yields the current value via ``source.get()``.
    ``COMPLETE`` / ``ERROR`` / ``TEARDOWN`` end the iteration.
    Unsubscribes on ``break`` / ``aclose()``.

    Must be called from a running asyncio event loop.

    Note:
        ``state()`` nodes do not emit ``DATA`` on subscribe — they only emit
        when :meth:`~graphrefly.core.node.NodeImpl.set` is called.  For such
        nodes, the iterator will block until the first update arrives.
        Derived nodes emit ``DATA`` on subscribe when they recompute.

    Example::

        from graphrefly.extra import of
        from graphrefly.compat import to_async_iter

        async for value in to_async_iter(of(1, 2, 3)):
            print(value)

    Yields:
        Each ``DATA`` payload or current value on ``RESOLVED`` from the source.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def _enqueue(tag: str, payload: Any) -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(queue.put_nowait, (tag, payload))

    def sink(messages: Messages) -> None:
        for msg in messages:
            t = msg[0]
            if t is MessageType.DATA:
                _enqueue("DATA", msg[1] if len(msg) > 1 else None)
            elif t is MessageType.RESOLVED:
                _enqueue("DATA", source.get())
            elif t is MessageType.ERROR:
                err = msg[1] if len(msg) > 1 else RuntimeError("node error")
                _enqueue("ERROR", err)
            elif t is MessageType.COMPLETE or t is MessageType.TEARDOWN:
                _enqueue("DONE", None)

    unsub = source.subscribe(sink)
    try:
        while True:
            tag, payload = await queue.get()
            if tag == "DATA":
                yield payload
            elif tag == "ERROR":
                raise payload
            else:  # DONE
                return
    finally:
        unsub()


async def first_value_from_async(source: Node[Any]) -> Any:
    """Await the first ``DATA`` value from *source*.

    If the node already has a settled cached value, returns it immediately
    without subscribing.  Otherwise subscribes reactively and waits.

    Raises:
        RuntimeError: If the source completes without emitting ``DATA``.
        Exception: If the source emits ``ERROR``.

    Example::

        from graphrefly import state
        from graphrefly.compat import first_value_from_async

        s = state(42)
        value = await first_value_from_async(s)
        assert value == 42
    """
    # Fast path: already settled with a cached value.
    # Uses NO_VALUE sentinel so ``None`` as a real domain value is not skipped.
    status = source.status
    if status in ("settled", "resolved"):
        v = getattr(source, "_cached", NO_VALUE)
        if v is not NO_VALUE:
            return v

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def _set_result(value: Any) -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_resolve, future, value)

    def _set_error(err: BaseException) -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_reject, future, err)

    def sink(messages: Messages) -> None:
        for msg in messages:
            t = msg[0]
            if t is MessageType.DATA:
                _set_result(msg[1] if len(msg) > 1 else None)
                return
            if t is MessageType.ERROR:
                err = msg[1] if len(msg) > 1 else RuntimeError("node error")
                _set_error(err)
                return
            if t is MessageType.COMPLETE or t is MessageType.TEARDOWN:
                _set_error(RuntimeError("source completed without DATA"))
                return

    unsub = source.subscribe(sink)
    try:
        return await future
    finally:
        unsub()


async def settled(source: Node[Any]) -> Any:
    """Await until *source* has a settled (non-dirty) value.

    If the node already holds a cached value and is in a settled/resolved
    status, returns it without waiting.  Otherwise subscribes and waits for
    the first ``DATA`` or ``RESOLVED`` message.

    Raises:
        RuntimeError: If the source completes or tears down without settling.
        Exception: If the source emits ``ERROR``.

    Example::

        from graphrefly import derived, state
        from graphrefly.compat import settled

        a = state(10)
        b = derived([a], lambda deps, _: deps[0] * 2)
        value = await settled(b)
        assert value == 20
    """
    # Fast path: already settled — uses NO_VALUE sentinel so ``None`` is not skipped.
    status = source.status
    if status in ("settled", "resolved"):
        v = getattr(source, "_cached", NO_VALUE)
        if v is not NO_VALUE:
            return v

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def _set_result(value: Any) -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_resolve, future, value)

    def _set_error(err: BaseException) -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_reject, future, err)

    def sink(messages: Messages) -> None:
        for msg in messages:
            t = msg[0]
            if t is MessageType.DATA:
                _set_result(msg[1] if len(msg) > 1 else None)
                return
            if t is MessageType.RESOLVED:
                _set_result(source.get())
                return
            if t is MessageType.ERROR:
                err = msg[1] if len(msg) > 1 else RuntimeError("node error")
                _set_error(err)
                return
            if t is MessageType.COMPLETE or t is MessageType.TEARDOWN:
                _set_error(RuntimeError("source completed without settling"))
                return

    unsub = source.subscribe(sink)
    try:
        return await future
    finally:
        unsub()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve(future: asyncio.Future[Any], value: Any) -> None:
    if not future.done():
        future.set_result(value)


def _reject(future: asyncio.Future[Any], err: BaseException) -> None:
    if not future.done():
        future.set_exception(err)


__all__ = [
    "first_value_from_async",
    "settled",
    "to_async_iter",
]
