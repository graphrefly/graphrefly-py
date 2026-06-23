import asyncio
from collections.abc import Awaitable, Callable
from threading import get_ident

import pytest

from graphrefly import (
    SENTINEL,
    CallbackError,
    ControlMessage,
    Ctx,
    DataMessage,
    ErrorMessage,
    Graph,
    GraphReflyRuntimeError,
    GraphReflyValueError,
    Message,
    async_node,
    asyncio_runner,
    from_async_iter,
    from_awaitable,
)
from graphrefly._facade import _AsyncJob, _GraphLifetime


class ManualTask:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class ManualRunner:
    def __init__(self) -> None:
        self.jobs: list[Callable[[], Awaitable[None]]] = []
        self.tasks: list[ManualTask] = []

    def spawn(self, job: Callable[[], Awaitable[None]]) -> ManualTask:
        task = ManualTask()
        self.jobs.append(job)
        self.tasks.append(task)
        return task

    def cancel(self, task: object | None) -> None:
        if isinstance(task, ManualTask):
            task.cancel()

    def run(self, index: int) -> None:
        asyncio.run(self.jobs[index]())


def test_from_awaitable_source_emits_data_then_complete():
    graph = Graph("py-from-awaitable-smoke")
    runner = ManualRunner()
    seen: list[Message[object]] = []
    calls = 0

    def factory() -> Awaitable[int]:
        nonlocal calls
        calls += 1

        async def work() -> int:
            return 42

        return work()

    node = from_awaitable(graph, runner, factory, name="awaitable")
    with node.subscribe(seen.append):
        assert calls == 0
        assert len(runner.jobs) == 1
        runner.run(0)
        assert node.cache() == 42

    assert calls == 1
    assert [msg.kind for msg in seen[-2:]] == ["DATA", "COMPLETE"]
    assert DataMessage(42) in seen
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_from_async_iter_source_emits_yields_then_complete():
    graph = Graph("py-from-async-iter-smoke")
    runner = ManualRunner()
    seen: list[Message[object]] = []

    async def values():
        yield "a"
        yield "b"

    node = from_async_iter(graph, runner, values, name="aiter")
    with node.subscribe(seen.append):
        runner.run(0)
        assert node.cache() == "b"

    assert [msg.value for msg in seen if isinstance(msg, DataMessage)] == ["a", "b"]
    assert seen[-1] == ControlMessage("COMPLETE")


def test_asyncio_runner_uses_caller_owned_loop():
    async def main() -> None:
        graph = Graph("py-asyncio-runner-smoke")
        seen: list[Message[object]] = []

        async def work() -> int:
            return 9

        node = from_awaitable(graph, asyncio_runner(), work, name="asyncio_source")
        with node.subscribe(seen.append):
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert node.cache() == 9

        assert DataMessage(9) in seen

    asyncio.run(main())


def test_asyncio_runner_closes_coroutine_when_create_task_fails(recwarn):
    loop = asyncio.new_event_loop()
    loop.close()
    runner = asyncio_runner(loop)

    async def body() -> None:
        return None

    with pytest.raises(RuntimeError):
        runner.spawn(body)

    leaked = [
        warning
        for warning in recwarn
        if issubclass(warning.category, RuntimeWarning)
        and "never awaited" in str(warning.message)
    ]
    assert leaked == []


def test_async_node_stale_generation_completion_is_ignored():
    graph = Graph("py-async-node-generation-smoke")
    runner = ManualRunner()
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    async def compute(value: int) -> int:
        return value * 10

    node = async_node(graph, [source], runner, compute, name="async_compute")
    with node.subscribe(seen.append):
        assert len(runner.jobs) == 1
        source.set(2)
        assert len(runner.jobs) == 2
        runner.run(1)
        assert node.cache() == 20
        runner.run(0)
        assert node.cache() == 20

    assert [msg.value for msg in seen if isinstance(msg, DataMessage)] == [20]


def test_async_node_deactivation_cleans_up_pending_task_without_graph_error():
    graph = Graph("py-async-node-cleanup-smoke")
    runner = ManualRunner()
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    async def compute(value: int) -> int:
        return value + 1

    node = async_node(graph, [source], runner, compute, name="async_cleanup")
    sub = node.subscribe(seen.append)
    assert len(runner.tasks) == 1
    sub.unsubscribe()

    assert runner.tasks[0].cancelled is True
    runner.run(0)
    assert not node.has_value
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_async_inputs_must_be_factory_shaped():
    graph = Graph("py-async-factory-shape-smoke")
    runner = ManualRunner()

    async def work() -> int:
        return 1

    coroutine = work()
    try:
        with pytest.raises(GraphReflyValueError, match="factory callables"):
            from_awaitable(graph, runner, coroutine)  # type: ignore[arg-type]
    finally:
        coroutine.close()


def test_sync_callback_surfaces_reject_coroutine_functions_at_registration():
    graph = Graph("py-sync-registration-rejects-async-smoke")
    source = graph.state(1, name="source")

    async def async_value(_value: int = 1) -> int:
        return _value

    async def async_ctx(_ctx: Ctx) -> None:
        return None

    async def async_batch() -> None:
        return None

    async def async_subscriber(_msg: Message[object]) -> None:
        return None

    async def async_observer(_event: object) -> None:
        return None

    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        graph.producer(async_value)
    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        graph.derived([source], async_value)
    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        graph.effect([source], async_value)
    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        graph.node([source], async_ctx)
    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        graph.batch(async_batch)
    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        source.subscribe(async_subscriber)
    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        graph.observe(async_observer)


def test_sync_data_ingress_rejects_awaitables_without_leaking_warning(recwarn):
    graph = Graph("py-sync-data-ingress-rejects-awaitables-smoke")

    async def work() -> int:
        return 1

    coroutine = work()
    with pytest.raises(CallbackError, match="async callbacks are deferred"):
        graph.state(coroutine)

    source = graph.state(1, name="source")
    coroutine = work()
    with pytest.raises(CallbackError, match="async callbacks are deferred"):
        source.set(coroutine)  # type: ignore[arg-type]

    leaked = [
        warning
        for warning in recwarn
        if issubclass(warning.category, RuntimeWarning)
        and "never awaited" in str(warning.message)
    ]
    assert leaked == []


def test_ctx_state_and_pull_params_reject_async_material_without_leaking_warning(recwarn):
    graph = Graph("py-ctx-async-material-rejects-smoke")
    source = graph.state(1, name="source")

    async def work() -> int:
        return 1

    def state_body(ctx: Ctx) -> None:
        ctx.state = work()

    def pull_body(ctx: Ctx) -> None:
        ctx.request_pull("pull", work())

    def sentinel_body(ctx: Ctx) -> None:
        ctx.state = SENTINEL

    state_node = graph.node([source], state_body, name="bad_state")
    pull_node = graph.node([source], pull_body, name="bad_pull")
    sentinel_node = graph.node([source], sentinel_body, name="bad_sentinel")

    seen: list[Message[object]] = []
    with (
        state_node.subscribe(seen.append),
        pull_node.subscribe(seen.append),
        sentinel_node.subscribe(seen.append),
    ):
        pass

    errors = [msg for msg in seen if isinstance(msg, ErrorMessage)]
    assert any("async callbacks are deferred" in msg.error.message for msg in errors)
    assert any("SENTINEL" in msg.error.message for msg in errors)
    leaked = [
        warning
        for warning in recwarn
        if issubclass(warning.category, RuntimeWarning)
        and "never awaited" in str(warning.message)
    ]
    assert leaked == []


def test_async_job_start_cancels_task_if_lifetime_closed_during_spawn():
    lifetime = _GraphLifetime(get_ident())
    task = ManualTask()

    class ClosingRunner:
        def spawn(self, _job: Callable[[], Awaitable[None]]) -> ManualTask:
            lifetime.close()
            return task

        def cancel(self, task: object | None) -> None:
            if isinstance(task, ManualTask):
                task.cancel()

    async def body() -> None:
        return None

    job = _AsyncJob(ClosingRunner(), lifetime)
    job.start(body)

    assert task.cancelled is True
    assert job.active is False
