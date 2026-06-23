import asyncio
from collections.abc import Awaitable, Callable
from threading import Event, Thread, get_ident

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
from graphrefly._facade import _AsyncJob, _GraphLifetime, _GraphReentryCompletion


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


class ThreadTask:
    def __init__(self, thread: Thread) -> None:
        self.thread = thread
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class ThreadedRunner:
    def __init__(self) -> None:
        self.tasks: list[ThreadTask] = []
        self.errors: list[BaseException] = []
        self.worker_threads: list[int] = []

    def spawn(self, job: Callable[[], Awaitable[None]]) -> ThreadTask:
        task: ThreadTask

        def target() -> None:
            self.worker_threads.append(get_ident())
            try:
                asyncio.run(job())
            except BaseException as error:
                self.errors.append(error)

        thread = Thread(target=target)
        task = ThreadTask(thread)
        self.tasks.append(task)
        thread.start()
        return task

    def cancel(self, task: object | None) -> None:
        if isinstance(task, ThreadTask):
            task.cancel()

    def join(self, index: int = 0) -> None:
        self.tasks[index].thread.join(timeout=5)
        assert not self.tasks[index].thread.is_alive()
        assert self.errors == []


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


def test_reentry_queue_enqueues_cross_thread_completion_and_owner_thread_drain_applies_it():
    graph = Graph("py-reentry-cross-thread-smoke")
    queue = graph.reentry_queue()
    runner = ThreadedRunner()
    seen: list[Message[object]] = []

    async def work() -> int:
        return 42

    node = from_awaitable(graph, queue.wrap_runner(runner), work, name="queued_awaitable")
    with node.subscribe(seen.append):
        runner.join()
        assert runner.worker_threads[0] != get_ident()
        assert queue.pending_count == 1
        assert not node.has_value
        assert queue.drain() == 1
        assert node.cache() == 42

    assert [msg.kind for msg in seen[-2:]] == ["DATA", "COMPLETE"]
    assert DataMessage(42) in seen


def test_reentry_queue_drain_rejects_non_owner_thread():
    graph = Graph("py-reentry-owner-thread-smoke")
    queue = graph.reentry_queue()
    errors: list[BaseException] = []

    def target() -> None:
        try:
            queue.drain()
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=target)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], GraphReflyRuntimeError)
    assert "owner thread" in str(errors[0])
    assert queue.drain() == 0


def test_reentry_queue_wrapped_runner_must_belong_to_target_graph():
    graph_a = Graph("py-reentry-graph-a-smoke")
    graph_b = Graph("py-reentry-graph-b-smoke")
    runner = graph_a.reentry_queue().wrap_runner(ManualRunner())

    async def work() -> int:
        return 1

    with pytest.raises(GraphReflyRuntimeError, match="target Graph"):
        from_awaitable(graph_b, runner, work, name="wrong_graph_source")

    source = graph_b.state(1, name="source")
    with pytest.raises(GraphReflyRuntimeError, match="target Graph"):
        async_node(graph_b, [source], runner, work, name="wrong_graph_node")


def test_reentry_queue_closed_wrapped_runner_is_rejected_before_activation():
    graph = Graph("py-reentry-closed-runner-smoke")
    queue = graph.reentry_queue()
    runner = queue.wrap_runner(ManualRunner())
    queue.close()

    async def work() -> int:
        return 1

    with pytest.raises(GraphReflyRuntimeError, match="queue is closed"):
        from_awaitable(graph, runner, work, name="closed_queue_source")


def test_reentry_queue_closed_after_node_construction_does_not_spawn_on_activation():
    graph = Graph("py-reentry-close-before-activation-smoke")
    queue = graph.reentry_queue()
    runner = ThreadedRunner()
    seen: list[Message[object]] = []

    async def work() -> int:
        return 1

    node = from_awaitable(graph, queue.wrap_runner(runner), work, name="close_before_sub")
    queue.close()

    with node.subscribe(seen.append):
        pass

    assert runner.tasks == []
    assert queue.pending_count == 0
    assert not node.has_value
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_reentry_queue_drain_respects_max_items_fifo():
    graph = Graph("py-reentry-max-items-smoke")
    queue = graph.reentry_queue()
    runner = ThreadedRunner()
    seen: list[Message[object]] = []

    async def values():
        yield "a"
        yield "b"

    node = from_async_iter(graph, queue.wrap_runner(runner), values, name="queued_iter")
    with node.subscribe(seen.append):
        runner.join()
        assert queue.pending_count == 3
        assert queue.drain(max_items=2) == 2
        assert [msg.value for msg in seen if isinstance(msg, DataMessage)] == ["a", "b"]
        assert queue.pending_count == 1
        assert queue.drain(max_items=1) == 1

    assert seen[-1] == ControlMessage("COMPLETE")


def test_reentry_queue_graph_close_drops_pending_completions_without_graph_error():
    graph = Graph("py-reentry-close-cleanup-smoke")
    queue = graph.reentry_queue()
    runner = ThreadedRunner()
    seen: list[Message[object]] = []

    async def work() -> int:
        return 7

    node = from_awaitable(graph, queue.wrap_runner(runner), work, name="queued_close")
    sub = node.subscribe(seen.append)
    runner.join()
    assert queue.pending_count == 1

    graph.close()

    assert graph.closed is True
    assert sub.closed is True
    assert queue.closed is True
    assert queue.pending_count == 0
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_reentry_queue_fatal_completion_poison_closes_without_graph_error():
    graph = Graph("py-reentry-fatal-cleanup-smoke")
    queue = graph.reentry_queue()
    runner = ThreadedRunner()
    seen: list[Message[object]] = []

    async def work() -> int:
        raise KeyboardInterrupt("stop")

    node = from_awaitable(graph, queue.wrap_runner(runner), work, name="queued_fatal")
    sub = node.subscribe(seen.append)
    runner.join()
    assert queue.pending_count == 1

    with pytest.raises(KeyboardInterrupt, match="stop"):
        queue.drain()

    assert graph.closed is True
    assert sub.closed is True
    assert queue.closed is True
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_reentry_queue_deactivation_drops_pending_completion_without_graph_error():
    graph = Graph("py-reentry-deactivation-cleanup-smoke")
    queue = graph.reentry_queue()
    runner = ThreadedRunner()
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []
    entered = Event()
    release = Event()

    async def compute(value: int) -> int:
        entered.set()
        assert release.wait(timeout=5)
        return value + 1

    node = async_node(graph, [source], queue.wrap_runner(runner), compute, name="queued_cleanup")
    sub = node.subscribe(seen.append)
    assert entered.wait(timeout=5)
    assert queue.pending_count == 0

    sub.unsubscribe()

    assert runner.tasks[0].cancelled is True
    release.set()
    runner.join()
    assert queue.drain() == 0
    assert not node.has_value
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_reentry_queue_close_cancels_active_queue_owned_job_without_graph_error():
    graph = Graph("py-reentry-close-active-job-smoke")
    queue = graph.reentry_queue()
    runner = ThreadedRunner()
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []
    entered = Event()
    release = Event()

    async def compute(value: int) -> int:
        entered.set()
        assert release.wait(timeout=5)
        return value + 1

    node = async_node(graph, [source], queue.wrap_runner(runner), compute, name="queued_active")
    with node.subscribe(seen.append):
        assert entered.wait(timeout=5)
        queue.close()
        assert runner.tasks[0].cancelled is True
        release.set()
        runner.join()
        assert queue.pending_count == 0
        assert not node.has_value

    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_async_node_stale_generation_completion_is_ignored_after_queue_drain():
    graph = Graph("py-reentry-async-node-generation-smoke")
    queue = graph.reentry_queue()
    runner = ThreadedRunner()
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    async def compute(value: int) -> int:
        return value * 10

    node = async_node(graph, [source], queue.wrap_runner(runner), compute, name="queued_compute")
    with node.subscribe(seen.append):
        runner.join(0)
        source.set(2)
        runner.join(1)
        assert queue.pending_count == 2
        assert queue.drain(max_items=1) == 1
        assert not node.has_value
        assert queue.drain() == 1
        assert node.cache() == 20

    assert [msg.value for msg in seen if isinstance(msg, DataMessage)] == [20]


def test_reentry_queue_stale_final_completion_unregisters_gate():
    graph = Graph("py-reentry-stale-gate-cleanup-smoke")
    queue = graph.reentry_queue()
    runner = ThreadedRunner()
    source = graph.state(1, name="source")

    async def compute(value: int) -> int:
        return value * 10

    node = async_node(graph, [source], queue.wrap_runner(runner), compute, name="queued_stale")
    with node.subscribe(lambda _msg: None):
        runner.join(0)
        source.set(2)
        runner.join(1)
        assert len(queue._gates) == 2
        assert queue.drain(max_items=1) == 1
        assert len(queue._gates) == 1
        assert queue.drain() == 1
        assert len(queue._gates) == 0


def test_reentry_queue_drain_continues_after_non_final_failure_for_terminal_cleanup():
    graph = Graph("py-reentry-drain-cleanup-after-error-smoke")
    queue = graph.reentry_queue()
    calls: list[str] = []

    class FakeGate:
        def _emit_now(
            self,
            _value: object,
            _should_apply: object,
            *,
            final: bool,
        ) -> bool:
            calls.append(f"emit:{final}")
            raise RuntimeError("emit failed")

        def _complete_now(self, _should_apply: object, *, final: bool) -> bool:
            calls.append(f"complete:{final}")
            return True

    queue._gates[0] = FakeGate()  # type: ignore[assignment]
    queue._items.append(
        _GraphReentryCompletion(gate_id=0, op="emit", value="bad", final=False)
    )
    queue._items.append(_GraphReentryCompletion(gate_id=0, op="complete", final=True))

    with pytest.raises(RuntimeError, match="emit failed"):
        queue.drain()

    assert calls == ["emit:False", "complete:True"]
    assert len(queue._gates) == 0
    assert queue.pending_count == 0


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
    graph.reentry_queue()
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
