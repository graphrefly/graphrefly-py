"""Tests for the runner protocol, AsyncioRunner, and async utilities (roadmap §5.1)."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from graphrefly import state
from graphrefly.compat import AsyncioRunner, first_value_from_async, settled, to_async_iter
from graphrefly.core.protocol import MessageType
from graphrefly.core.runner import (
    Runner,
    get_default_runner,
    resolve_runner,
    set_default_runner,
)
from graphrefly.extra.sources import from_any, from_async_iter, from_awaitable

# ---------------------------------------------------------------------------
# Runner protocol
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """Test runner that records scheduled coroutines and runs them on asyncio."""

    def __init__(self) -> None:
        self.scheduled: list[Any] = []

    def schedule(
        self,
        coro: Any,
        on_result: Any,
        on_error: Any,
    ) -> Any:
        self.scheduled.append(coro)

        async def _run() -> None:
            try:
                result = await coro
            except BaseException as err:
                on_error(err)
            else:
                on_result(result)

        t = threading.Thread(target=lambda: asyncio.run(_run()), daemon=True)
        t.start()

        def cancel() -> None:
            pass

        return cancel


def test_recording_runner_is_runner() -> None:
    assert isinstance(_RecordingRunner(), Runner)


# ---------------------------------------------------------------------------
# Default runner management
# ---------------------------------------------------------------------------


def test_default_runner_raises_when_unset() -> None:
    set_default_runner(None)
    with pytest.raises(RuntimeError, match="No Runner configured"):
        get_default_runner()


def test_set_and_get_default_runner() -> None:
    rec = _RecordingRunner()
    set_default_runner(rec)
    try:
        assert get_default_runner() is rec
    finally:
        set_default_runner(None)


def test_resolve_runner_explicit() -> None:
    rec = _RecordingRunner()
    assert resolve_runner(rec) is rec


def test_resolve_runner_default_raises() -> None:
    set_default_runner(None)
    with pytest.raises(RuntimeError, match="No Runner configured"):
        resolve_runner(None)


# ---------------------------------------------------------------------------
# AsyncioRunner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asyncio_runner_from_running() -> None:
    runner = AsyncioRunner.from_running()
    assert isinstance(runner, AsyncioRunner)


@pytest.mark.asyncio
async def test_asyncio_runner_schedule_success() -> None:
    runner = AsyncioRunner.from_running()
    future: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def coro() -> int:
        return 42

    runner.schedule(
        coro(),
        lambda v: future.get_loop().call_soon_threadsafe(future.set_result, v),
        lambda e: future.get_loop().call_soon_threadsafe(future.set_exception, e),
    )
    result = await asyncio.wait_for(future, timeout=5)
    assert result == 42


@pytest.mark.asyncio
async def test_asyncio_runner_schedule_error() -> None:
    runner = AsyncioRunner.from_running()
    future: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def coro() -> int:
        raise ValueError("async boom")

    runner.schedule(
        coro(),
        lambda v: future.get_loop().call_soon_threadsafe(future.set_result, v),
        lambda e: future.get_loop().call_soon_threadsafe(future.set_exception, e),
    )
    with pytest.raises(ValueError, match="async boom"):
        await asyncio.wait_for(future, timeout=5)


@pytest.mark.asyncio
async def test_asyncio_runner_cancel() -> None:
    runner = AsyncioRunner.from_running()
    results: list[int] = []

    async def coro() -> int:
        await asyncio.sleep(10)
        return 99

    cancel = runner.schedule(coro(), lambda v: results.append(v), lambda _: None)
    await asyncio.sleep(0.05)
    cancel()
    await asyncio.sleep(0.1)
    assert results == []


@pytest.mark.asyncio
async def test_asyncio_runner_cancel_before_task_created() -> None:
    """Cancel before the event loop creates the task — the coroutine should not run."""
    runner = AsyncioRunner.from_running()
    results: list[int] = []
    errors: list[Any] = []

    async def coro() -> int:
        return 42

    cancel = runner.schedule(coro(), lambda v: results.append(v), lambda e: errors.append(e))
    cancel()  # Cancel immediately, before event loop tick.
    await asyncio.sleep(0.1)
    assert results == []
    assert errors == []


# ---------------------------------------------------------------------------
# from_awaitable / from_async_iter with runner
# ---------------------------------------------------------------------------


def test_from_awaitable_with_custom_runner() -> None:
    rec = _RecordingRunner()

    async def coro() -> int:
        return 7

    n = from_awaitable(coro(), runner=rec)
    # Subscribe to trigger the producer.
    received: list[Any] = []

    def sink(msgs: Any) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                received.append(m[1])

    unsub = n.subscribe(sink)
    import time

    time.sleep(0.3)
    unsub()
    assert len(rec.scheduled) == 1
    assert received == [7]


def test_from_async_iter_with_custom_runner() -> None:
    rec = _RecordingRunner()

    async def gen() -> Any:
        for i in range(3):
            yield i

    n = from_async_iter(gen(), runner=rec)
    received: list[Any] = []

    def sink(msgs: Any) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                received.append(m[1])

    unsub = n.subscribe(sink)
    import time

    time.sleep(0.3)
    unsub()
    assert len(rec.scheduled) == 1
    assert received == [0, 1, 2]


def test_from_awaitable_uses_default_runner() -> None:
    rec = _RecordingRunner()
    set_default_runner(rec)
    try:

        async def coro() -> int:
            return 99

        n = from_awaitable(coro())
        received: list[Any] = []

        def sink(msgs: Any) -> None:
            for m in msgs:
                if m[0] is MessageType.DATA:
                    received.append(m[1])

        unsub = n.subscribe(sink)
        import time

        time.sleep(0.3)
        unsub()
        assert len(rec.scheduled) == 1
        assert received == [99]
    finally:
        set_default_runner(None)


def test_from_any_forwards_runner() -> None:
    rec = _RecordingRunner()

    async def coro() -> int:
        return 55

    n = from_any(coro(), runner=rec)
    received: list[Any] = []

    def sink(msgs: Any) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                received.append(m[1])

    unsub = n.subscribe(sink)
    import time

    time.sleep(0.3)
    unsub()
    assert len(rec.scheduled) == 1
    assert received == [55]


# ---------------------------------------------------------------------------
# Async utilities: to_async_iter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_to_async_iter_basic() -> None:
    from graphrefly.extra import of

    values: list[Any] = []
    async for v in to_async_iter(of(10, 20)):
        values.append(v)
    assert values == [10, 20]


@pytest.mark.asyncio
async def test_to_async_iter_multiple_values() -> None:
    """Use from_async_iter to emit multiple values via an async generator."""

    async def gen() -> Any:
        for i in [1, 2, 3]:
            yield i

    runner = AsyncioRunner.from_running()
    n = from_async_iter(gen(), runner=runner)
    values: list[Any] = []
    async for v in to_async_iter(n):
        values.append(v)
    assert values == [1, 2, 3]


@pytest.mark.asyncio
async def test_to_async_iter_error() -> None:
    runner = AsyncioRunner.from_running()

    async def bad() -> None:
        raise ValueError("oops")

    n = from_awaitable(bad(), runner=runner)
    with pytest.raises(ValueError, match="oops"):
        async for _ in to_async_iter(n):
            pass


# ---------------------------------------------------------------------------
# Async utilities: first_value_from_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_value_from_async_state() -> None:
    s = state(42)
    v = await first_value_from_async(s)
    assert v == 42


@pytest.mark.asyncio
async def test_first_value_from_async_falsy_value() -> None:
    """Fast path must work for falsy values like 0, False, empty string."""
    s = state(0)
    v = await first_value_from_async(s)
    assert v == 0

    s2 = state(False)
    v2 = await first_value_from_async(s2)
    assert v2 is False

    s3 = state("")
    v3 = await first_value_from_async(s3)
    assert v3 == ""


@pytest.mark.asyncio
async def test_first_value_from_async_error() -> None:
    runner = AsyncioRunner.from_running()

    async def bad() -> None:
        raise ValueError("fail")

    n = from_awaitable(bad(), runner=runner)
    with pytest.raises(ValueError, match="fail"):
        await first_value_from_async(n)


# ---------------------------------------------------------------------------
# Async utilities: settled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settled_state() -> None:
    s = state(100)
    v = await settled(s)
    assert v == 100


@pytest.mark.asyncio
async def test_settled_falsy_value() -> None:
    """Fast path must work for falsy values."""
    s = state(0)
    v = await settled(s)
    assert v == 0


@pytest.mark.asyncio
async def test_settled_derived() -> None:
    from graphrefly import derived

    a = state(5)
    b = derived([a], lambda deps, _: deps[0] * 3)
    v = await settled(b)
    assert v == 15


@pytest.mark.asyncio
async def test_settled_error() -> None:
    runner = AsyncioRunner.from_running()

    async def bad() -> None:
        raise ValueError("err")

    n = from_awaitable(bad(), runner=runner)
    with pytest.raises(ValueError, match="err"):
        await settled(n)


# ---------------------------------------------------------------------------
# TrioRunner (skipped when trio is not installed)
# ---------------------------------------------------------------------------

_has_trio = False
try:
    import trio as _trio_mod  # noqa: F401

    _has_trio = True
except ImportError:
    pass


@pytest.mark.skipif(not _has_trio, reason="trio not installed")
def test_trio_runner_schedule_success() -> None:
    import trio as _trio

    from graphrefly.compat.trio_runner import TrioRunner

    results: list[Any] = []

    async def main() -> None:
        async with _trio.open_nursery() as nursery:
            runner = TrioRunner(nursery)
            event = _trio.Event()

            async def coro() -> int:
                return 77

            def on_result(v: Any) -> None:
                results.append(v)
                event.set()

            runner.schedule(coro(), on_result, lambda _: None)
            with _trio.fail_after(5):
                await event.wait()

    _trio.run(main)
    assert results == [77]


@pytest.mark.skipif(not _has_trio, reason="trio not installed")
def test_trio_runner_schedule_error() -> None:
    import trio as _trio

    from graphrefly.compat.trio_runner import TrioRunner

    errors: list[Any] = []

    async def main() -> None:
        async with _trio.open_nursery() as nursery:
            runner = TrioRunner(nursery)
            event = _trio.Event()

            async def coro() -> None:
                raise ValueError("trio boom")

            def on_error(e: Any) -> None:
                errors.append(e)
                event.set()

            runner.schedule(coro(), lambda _: None, on_error)
            with _trio.fail_after(5):
                await event.wait()

    _trio.run(main)
    assert len(errors) == 1
    assert str(errors[0]) == "trio boom"
