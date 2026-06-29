from __future__ import annotations

import pytest

from graphrefly import (
    DataMessage,
    Graph,
    Message,
    anyio_runner,
    from_async_iter,
    from_awaitable,
    trio_runner,
)


def test_trio_runner_real_nursery_is_caller_owned_smoke() -> None:
    trio = pytest.importorskip("trio")

    async def main() -> None:
        graph = Graph("py-real-trio-runner-smoke")
        seen: list[Message[object]] = []

        async def work() -> int:
            await trio.lowlevel.checkpoint()
            return 31

        async with trio.open_nursery() as nursery:
            node = from_awaitable(
                graph,
                trio_runner(nursery),
                work,
                name="real_trio_source",
            )
            with node.subscribe(seen.append):
                for _ in range(10):
                    if node.has_value:
                        break
                    await trio.lowlevel.checkpoint()
                assert node.cache() == 31

        assert DataMessage(31) in seen

    trio.run(main)


def test_anyio_runner_real_task_group_is_caller_owned_smoke() -> None:
    anyio = pytest.importorskip("anyio")

    async def main() -> None:
        graph = Graph("py-real-anyio-runner-smoke")
        seen: list[Message[object]] = []

        async def values():
            await anyio.sleep(0)
            yield "a"
            await anyio.sleep(0)
            yield "b"

        async with anyio.create_task_group() as task_group:
            node = from_async_iter(
                graph,
                anyio_runner(task_group),
                values,
                name="real_anyio_source",
            )
            with node.subscribe(seen.append):
                for _ in range(10):
                    if len([msg for msg in seen if isinstance(msg, DataMessage)]) >= 2:
                        break
                    await anyio.sleep(0)
                assert node.cache() == "b"

        assert [msg.value for msg in seen if isinstance(msg, DataMessage)] == ["a", "b"]

    anyio.run(main)
