"""Async sources and runners for the Python facade (graphrefly-py:D2; D477/D480)."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from dataclasses import dataclass
from importlib import import_module
from inspect import isawaitable, iscoroutine
from threading import Lock, get_ident
from typing import Any, Literal, Protocol, TypeVar, cast
from weakref import ReferenceType, ref

from graphrefly import _kernel as _k
from graphrefly._facade import (
    Graph,
    Node,
    _callback_error,
    _gate,
    _GraphLifetime,
    _poison_on_fatal,
    _reject_awaitable,
    _reject_sentinel_data,
)
from graphrefly.exceptions import (
    GraphReflyRuntimeError,
    GraphReflyValueError,
)

R = TypeVar("R")


_GRAPH_REENTRY_QUEUE_TOKEN = object()


type AsyncJobFactory = Callable[[], Awaitable[None]]


type _CompletionPredicate = Callable[[], bool]


type _CancelScopeFactory = Callable[[], object]


class AsyncRunner(Protocol):
    """Framework-neutral async job runner supplied explicitly by the host."""

    def spawn(self, job: AsyncJobFactory) -> object:
        """Schedule one async job in the caller-owned runtime."""
        ...


class GraphReentryQueue:
    """Owner-thread drain gate for GraphReFly-owned async completions."""

    def __init__(
        self,
        *,
        owner_thread: int,
        lifetime: _GraphLifetime,
        _token: object | None = None,
    ) -> None:
        if _token is not _GRAPH_REENTRY_QUEUE_TOKEN:
            msg = "GraphReentryQueue objects must be created by Graph.reentry_queue()"
            raise GraphReflyRuntimeError(msg)
        self._owner_thread = owner_thread
        self._lifetime = lifetime
        self._items: deque[_GraphReentryCompletion] = deque()
        self._gates: dict[int, _AsyncCompletionGate] = {}
        self._jobs: list[ReferenceType[_AsyncJob]] = []
        self._next_gate_id = 0
        self._lock = Lock()
        self._closed = False
        lifetime.register_reentry_queue(self)

    @property
    def closed(self) -> bool:
        """Return whether this re-entry queue has been closed."""

        with self._lock:
            return self._closed

    @property
    def pending_count(self) -> int:
        """Return the number of private completions waiting to drain."""

        with self._lock:
            return len(self._items)

    def wrap_runner(self, runner: AsyncRunner) -> AsyncRunner:
        """Return an AsyncRunner adapter whose completions are drained by this queue."""

        self._check_owner_thread()
        self._check_open()
        _validate_runner(runner)
        return _GraphReentryRunner(runner, self)

    def drain(self, max_items: int | None = None) -> int:
        """Drain queued private completions on the graph owner thread."""

        self._check_owner_thread()
        self._check_open()
        if max_items is not None:
            if isinstance(max_items, bool) or not isinstance(max_items, int):
                msg = "max_items must be an int or None"
                raise GraphReflyValueError(msg)
            if max_items < 0:
                msg = "max_items must be non-negative"
                raise GraphReflyValueError(msg)
        drained = 0
        first_error: BaseException | None = None
        while max_items is None or drained < max_items:
            item = self._pop()
            if item is None:
                break
            try:
                self._apply(item)
            except BaseException as error:
                if first_error is None:
                    first_error = error
            drained += 1
        if first_error is not None:
            raise first_error
        return drained

    def close(self) -> None:
        """Close this queue and cancel queued private completions."""

        self._check_owner_thread()
        self._close_from_graph()
        self._lifetime.unregister_reentry_queue(self)

    def _enqueue(self, item: _GraphReentryCompletion) -> bool:
        if self._lifetime.is_closed():
            return False
        with self._lock:
            if self._closed:
                return False
            self._items.append(item)
            return True

    def _register_job(self, job: _AsyncJob) -> None:
        if self._lifetime.is_closed():
            job.cancel()
            return
        cancel = False
        with self._lock:
            if self._closed:
                cancel = True
            else:
                self._jobs.append(ref(job))
        if cancel:
            job.cancel()

    def _unregister_job(self, job: _AsyncJob) -> None:
        with self._lock:
            self._jobs = [
                item for item in self._jobs if (live := item()) is not None and live is not job
            ]

    def _register_gate(self, gate: _AsyncCompletionGate) -> int:
        if self._lifetime.is_closed():
            return -1
        with self._lock:
            if self._closed:
                return -1
            gate_id = self._next_gate_id
            self._next_gate_id += 1
            self._gates[gate_id] = gate
            return gate_id

    def _unregister_gate(self, gate_id: int) -> None:
        if gate_id < 0:
            return
        with self._lock:
            self._gates.pop(gate_id, None)

    def _apply(self, item: _GraphReentryCompletion) -> None:
        gate = self._gate(item.gate_id)
        if gate is None:
            return
        try:
            if item.op == "emit":
                gate._emit_now(item.value, item.should_apply, final=item.final)
            elif item.op == "complete":
                gate._complete_now(item.should_apply, final=item.final)
            elif item.op == "resolve":
                gate._resolve_now(item.value, item.should_apply, final=item.final)
            elif item.op == "error":
                assert isinstance(item.value, BaseException)
                gate._error_now(item.value, item.should_apply, final=item.final)
            else:
                assert isinstance(item.value, BaseException)
                gate._fatal_now(item.value, item.should_apply, final=item.final)
        finally:
            if item.final:
                self._unregister_gate(item.gate_id)

    def _gate(self, gate_id: int) -> _AsyncCompletionGate | None:
        with self._lock:
            return self._gates.get(gate_id)

    def _pop(self) -> _GraphReentryCompletion | None:
        with self._lock:
            if not self._items:
                return None
            return self._items.popleft()

    def _close_from_graph(self) -> None:
        with self._lock:
            self._closed = True
            self._items.clear()
            self._gates.clear()
            jobs = tuple(item() for item in self._jobs)
            self._jobs.clear()
        for job in jobs:
            if job is not None:
                job.cancel()

    def _can_accept_activation(self) -> bool:
        if self._lifetime.is_closed():
            return False
        with self._lock:
            return not self._closed

    def _check_owner_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly reentry queues may only be drained by the graph owner thread"
            raise GraphReflyRuntimeError(msg)

    def _check_open(self) -> None:
        if self._lifetime.is_closed():
            raise GraphReflyRuntimeError(self._lifetime.closed_message)
        if self._closed:
            msg = "GraphReFly reentry queue is closed"
            raise GraphReflyRuntimeError(msg)


class _GraphReentryRunner:
    def __init__(self, runner: AsyncRunner, queue: GraphReentryQueue) -> None:
        self._runner = runner
        self._queue = queue

    def spawn(self, job: AsyncJobFactory) -> object:
        return self._runner.spawn(job)

    def cancel(self, task: object | None) -> None:
        runner_cancel = getattr(self._runner, "cancel", None)
        if callable(runner_cancel):
            result = runner_cancel(task)
            if isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
            return
        task_cancel = getattr(task, "cancel", None)
        if callable(task_cancel):
            result = task_cancel()
            if isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()


@dataclass(slots=True)
class _GraphReentryCompletion:
    gate_id: int
    op: Literal["emit", "complete", "resolve", "error", "fatal"]
    value: object | BaseException | None = None
    should_apply: _CompletionPredicate | None = None
    final: bool = True


class _AsyncCompletionGate:
    """Owner-thread gate from a runner's completion to the node's lifetime ctx.

    A completion becomes ``ctx.down`` after the invocation returned, so it is a later
    wave of the node; once the lifetime has closed it is dropped (R-lifetime).
    """

    def __init__(
        self,
        ctx: _k.Ctx,
        *,
        owner_thread: int,
        lifetime: _GraphLifetime,
        reentry_queue: GraphReentryQueue | None = None,
    ) -> None:
        self._ctx = ctx
        self._owner_thread = owner_thread
        self._lifetime = lifetime
        self._reentry_queue = reentry_queue
        self._reentry_gate_id = (
            reentry_queue._register_gate(self) if reentry_queue is not None else -1
        )
        self._closed = False

    def queued_proxy(self) -> _QueuedCompletionGate:
        if self._reentry_queue is None or self._reentry_gate_id < 0:
            msg = "async completion gate is not registered with a reentry queue"
            raise GraphReflyRuntimeError(msg)
        return _QueuedCompletionGate(queue=self._reentry_queue, gate_id=self._reentry_gate_id)

    def emit(
        self,
        value: object,
        should_apply: _CompletionPredicate | None = None,
        *,
        final: bool = False,
    ) -> bool:
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        if self._should_queue():
            return self._queue("emit", value, should_apply, final=final)
        return self._emit_now(value, should_apply, final=final)

    def complete(self, should_apply: _CompletionPredicate | None = None) -> bool:
        if self._should_queue():
            return self._queue("complete", None, should_apply, final=True)
        return self._complete_now(should_apply, final=True)

    def resolve(self, value: object, should_apply: _CompletionPredicate | None = None) -> bool:
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        if self._should_queue():
            return self._queue("resolve", value, should_apply, final=True)
        return self._resolve_now(value, should_apply, final=True)

    def error(self, error: BaseException, should_apply: _CompletionPredicate | None = None) -> bool:
        if self._should_queue():
            return self._queue("error", error, should_apply, final=True)
        return self._error_now(error, should_apply, final=True)

    def fatal(self, error: BaseException, should_apply: _CompletionPredicate | None = None) -> bool:
        if self._should_queue():
            return self._queue("fatal", error, should_apply, final=True)
        return self._fatal_now(error, should_apply, final=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reentry_queue is not None:
            self._reentry_queue._unregister_gate(self._reentry_gate_id)

    def _down(
        self, array: list[_k.Message], should_apply: _CompletionPredicate | None, *, final: bool
    ) -> bool:
        try:
            if not self._can_reenter(should_apply):
                return False
            try:
                self._ctx.down(array)
            except _k.KernelError as error:
                raise GraphReflyRuntimeError(str(error)) from error
            except Exception:
                raise
            except BaseException as error:
                _poison_on_fatal(self._lifetime, error)
            return True
        finally:
            if final:
                self.close()

    def _emit_now(
        self,
        value: object,
        should_apply: _CompletionPredicate | None = None,
        *,
        final: bool = False,
    ) -> bool:
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        return self._down([(_k.DATA, value)], should_apply, final=final)

    def _complete_now(
        self, should_apply: _CompletionPredicate | None = None, *, final: bool = True
    ) -> bool:
        return self._down([(_k.COMPLETE,)], should_apply, final=final)

    def _resolve_now(
        self, value: object, should_apply: _CompletionPredicate | None = None, *, final: bool = True
    ) -> bool:
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        return self._down([(_k.DATA, value), (_k.COMPLETE,)], should_apply, final=final)

    def _error_now(
        self,
        error: BaseException,
        should_apply: _CompletionPredicate | None = None,
        *,
        final: bool = True,
    ) -> bool:
        return self._down([(_k.ERROR, _callback_error(error))], should_apply, final=final)

    def _fatal_now(
        self,
        error: BaseException,
        should_apply: _CompletionPredicate | None = None,
        *,
        final: bool = True,
    ) -> bool:
        try:
            if not self._can_reenter(should_apply):
                return False
            _poison_on_fatal(self._lifetime, error)
        finally:
            if final:
                self.close()

    def _can_reenter(self, should_apply: _CompletionPredicate | None = None) -> bool:
        if get_ident() != self._owner_thread:
            msg = "async runner completion must re-enter GraphReFly on the graph owner thread"
            raise GraphReflyRuntimeError(msg)
        if self._closed or self._lifetime.is_closed():
            return False
        if should_apply is not None and not should_apply():
            return False
        return not self._ctx.signal.aborted

    def _should_queue(self) -> bool:
        return self._reentry_queue is not None and get_ident() != self._owner_thread

    def _queue(
        self,
        op: Literal["emit", "complete", "resolve", "error", "fatal"],
        value: object | BaseException | None,
        should_apply: _CompletionPredicate | None,
        *,
        final: bool,
    ) -> bool:
        if self._closed or self._lifetime.is_closed():
            return False
        if should_apply is not None and not should_apply():
            return False
        if self._reentry_queue is None or self._reentry_gate_id < 0:
            return False
        return self._reentry_queue._enqueue(
            _GraphReentryCompletion(
                gate_id=self._reentry_gate_id,
                op=op,
                value=value,
                should_apply=should_apply,
                final=final,
            )
        )


@dataclass(frozen=True, slots=True)
class _QueuedCompletionGate:
    queue: GraphReentryQueue
    gate_id: int

    def emit(
        self,
        value: object,
        should_apply: _CompletionPredicate | None = None,
        *,
        final: bool = False,
    ) -> bool:
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        return self._queue("emit", value, should_apply, final=final)

    def complete(self, should_apply: _CompletionPredicate | None = None) -> bool:
        return self._queue("complete", None, should_apply, final=True)

    def resolve(
        self,
        value: object,
        should_apply: _CompletionPredicate | None = None,
    ) -> bool:
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        return self._queue("resolve", value, should_apply, final=True)

    def error(
        self,
        error: BaseException,
        should_apply: _CompletionPredicate | None = None,
    ) -> bool:
        return self._queue("error", error, should_apply, final=True)

    def fatal(
        self,
        error: BaseException,
        should_apply: _CompletionPredicate | None = None,
    ) -> bool:
        return self._queue("fatal", error, should_apply, final=True)

    def _queue(
        self,
        op: Literal["emit", "complete", "resolve", "error", "fatal"],
        value: object | BaseException | None,
        should_apply: _CompletionPredicate | None,
        *,
        final: bool,
    ) -> bool:
        if should_apply is not None and not should_apply():
            if final:
                self.queue._unregister_gate(self.gate_id)
            return False
        return self.queue._enqueue(
            _GraphReentryCompletion(
                gate_id=self.gate_id,
                op=op,
                value=value,
                should_apply=should_apply,
                final=final,
            )
        )


class _AsyncJob:
    def __init__(
        self,
        runner: AsyncRunner,
        lifetime: _GraphLifetime,
        reentry_queue: GraphReentryQueue | None = None,
    ) -> None:
        self._runner = runner
        self._lifetime = lifetime
        self._reentry_queue = reentry_queue
        self._lock = Lock()
        self._task: object | None = None
        self._cancel_requested = False
        self._done = False
        lifetime.register_async_job(self)
        if reentry_queue is not None:
            reentry_queue._register_job(self)

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    @property
    def active(self) -> bool:
        with self._lock:
            return not self._done and not self._cancel_requested and not self._lifetime.is_closed()

    def is_active(self) -> bool:
        return self.active

    def can_apply_completion(self) -> bool:
        with self._lock:
            return not self._cancel_requested and not self._lifetime.is_closed()

    def start(self, body: AsyncJobFactory) -> None:
        try:
            task = _spawn_runner_job(self._runner, body)
        except Exception:
            self.finish()
            raise
        with self._lock:
            self._task = task
            should_cancel = self._cancel_requested or self._done or self._lifetime.is_closed()
        if should_cancel:
            _cancel_runner_task(self._runner, task)
            self.finish()

    def cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            if self._done:
                return
            task = self._task
        _cancel_runner_task(self._runner, task)
        self.finish()

    def finish(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        if self._reentry_queue is not None:
            self._reentry_queue._unregister_job(self)
        self._lifetime.unregister_async_job(self)


class _AsyncioRunner:
    def __init__(self, loop: object | None = None) -> None:
        self._loop = loop

    def spawn(self, job: AsyncJobFactory) -> object:
        import asyncio

        loop = self._loop
        if loop is None:
            loop = asyncio.get_running_loop()
        awaitable = job()
        try:
            return cast("Any", loop).create_task(awaitable)
        except Exception:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise

    def cancel(self, task: object | None) -> None:
        cancel = getattr(task, "cancel", None)
        if callable(cancel):
            cancel()


class _CancelScopeTask:
    def __init__(self) -> None:
        self._lock = Lock()
        self._cancel_requested = False
        self._cancel_scope: object | None = None

    def cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            cancel_scope = self._cancel_scope
        _cancel_scope(cancel_scope)

    def _set_cancel_scope(self, cancel_scope: object) -> bool:
        with self._lock:
            self._cancel_scope = cancel_scope
            cancel_requested = self._cancel_requested
        if cancel_requested:
            _cancel_scope(cancel_scope)
        return cancel_requested


class _StartSoonRunner:
    def __init__(
        self,
        owner: object,
        *,
        cancel_scope_factory: _CancelScopeFactory,
        owner_name: str,
    ) -> None:
        self._owner = owner
        self._cancel_scope_factory = cancel_scope_factory
        self._owner_name = owner_name
        self._owner_thread = get_ident()

    def spawn(self, job: AsyncJobFactory) -> object:
        self._check_runtime_thread()
        start_soon = getattr(self._owner, "start_soon", None)
        if not callable(start_soon):
            msg = f"{self._owner_name} must provide start_soon(async_fn, *args)"
            raise GraphReflyValueError(msg)
        task = _CancelScopeTask()
        start_soon(_run_with_cancel_scope, task, job, self._cancel_scope_factory)
        return task

    def cancel(self, task: object | None) -> None:
        self._check_runtime_thread()
        if isinstance(task, _CancelScopeTask):
            task.cancel()

    def _check_runtime_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = f"{self._owner_name} runner must be used on its owner thread"
            raise GraphReflyRuntimeError(msg)


async def _run_with_cancel_scope(
    task: _CancelScopeTask,
    job: AsyncJobFactory,
    cancel_scope_factory: _CancelScopeFactory,
) -> None:
    cancel_scope = cancel_scope_factory()
    with cast("Any", cancel_scope):
        if task._set_cancel_scope(cancel_scope):
            return
        await job()


def _cancel_scope(cancel_scope: object | None) -> None:
    cancel = getattr(cancel_scope, "cancel", None)
    if callable(cancel):
        cancel()


def _should_suppress_async_error(job: _AsyncJob, error: BaseException) -> bool:
    return job.cancel_requested or _is_cancellation_error(error)


type _AsyncBody = Callable[
    [_AsyncCompletionGate | _QueuedCompletionGate, _AsyncJob], AsyncJobFactory
]


def _start_async(
    graph: Graph,
    ctx: _k.Ctx,
    runner: AsyncRunner,
    reentry_queue: GraphReentryQueue | None,
    body: _AsyncBody,
) -> Callable[[], None] | None:
    """Start one GraphReFly-owned async job for a node lifetime; return its stop."""

    if reentry_queue is not None and not reentry_queue._can_accept_activation():
        return None
    gate = _AsyncCompletionGate(
        ctx,
        owner_thread=graph._owner_thread,
        lifetime=graph._lifetime,
        reentry_queue=reentry_queue,
    )
    job = _AsyncJob(runner, graph._lifetime, reentry_queue)
    if not job.active:
        gate.close()
        return None
    completion = gate.queued_proxy() if reentry_queue is not None else gate
    job.start(body(completion, job))

    def stop() -> None:
        gate.close()
        job.cancel()

    return stop


def _settle_async_error(
    gate: _AsyncCompletionGate | _QueuedCompletionGate, job: _AsyncJob, error: BaseException
) -> None:
    if _should_suppress_async_error(job, error):
        return
    if isinstance(error, Exception):
        gate.error(error, job.can_apply_completion)
    else:
        gate.fatal(error, job.can_apply_completion)


def from_awaitable[T](
    graph: Graph,
    runner: AsyncRunner,
    factory: Callable[[], Awaitable[T]],
    *,
    name: str | None = None,
) -> Node[T]:
    """Create a source from a fresh awaitable produced on each activation.

    It sends the awaited value and COMPLETE in one array, or ERROR. Its lifetime's
    close cancels the job.
    """

    _validate_async_source_args(graph, runner, factory)
    reentry_queue = _runner_reentry_queue(graph, runner)

    def body(gate: _AsyncCompletionGate | _QueuedCompletionGate, job: _AsyncJob) -> AsyncJobFactory:
        async def run() -> None:
            try:
                if not job.active:
                    return
                awaitable = factory()
                if not isawaitable(awaitable):
                    msg = "from_awaitable factory must return an awaitable"
                    raise GraphReflyRuntimeError(msg)
                value = await awaitable
                gate.resolve(value, job.can_apply_completion)
            except BaseException as error:
                _settle_async_error(gate, job, error)
            finally:
                job.finish()

        return run

    return cast(
        "Node[T]",
        graph._source(name, lambda ctx: _start_async(graph, ctx, runner, reentry_queue, body)),
    )


def from_async_iter[T](
    graph: Graph,
    runner: AsyncRunner,
    factory: Callable[[], AsyncIterable[T]],
    *,
    name: str | None = None,
) -> Node[T]:
    """Create a source from a fresh async iterable produced on each activation.

    Each yielded value is one DATA array; the end is COMPLETE, a failure ERROR.
    """

    _validate_async_source_args(graph, runner, factory)
    reentry_queue = _runner_reentry_queue(graph, runner)

    def body(gate: _AsyncCompletionGate | _QueuedCompletionGate, job: _AsyncJob) -> AsyncJobFactory:
        async def run() -> None:
            try:
                if not job.active:
                    return
                iterable = factory()
                if not hasattr(iterable, "__aiter__"):
                    msg = "from_async_iter factory must return an async iterable"
                    raise GraphReflyRuntimeError(msg)
                async for value in iterable:
                    if not job.active:
                        return
                    gate.emit(value, job.can_apply_completion)
                gate.complete(job.can_apply_completion)
            except BaseException as error:
                _settle_async_error(gate, job, error)
            finally:
                job.finish()

        return run

    return cast(
        "Node[T]",
        graph._source(name, lambda ctx: _start_async(graph, ctx, runner, reentry_queue, body)),
    )


def async_node[T](
    graph: Graph,
    deps: Iterable[Node[Any]],
    runner: AsyncRunner,
    callback: Callable[..., Awaitable[T]],
    *,
    name: str | None = None,
) -> Node[T]:
    """Create a value-level async compute node over declared dependencies.

    Each run on new values (derived's gate) starts the callback; its result is a
    later wave. A completion of a superseded run is ignored (a generation fence), and
    the lifetime's close cancels every run still going.
    """

    graph._check_thread()
    dep_list = list(deps)
    _reject_async_factory_instance(callback)
    if not callable(callback):
        msg = "async_node callback must be callable"
        raise GraphReflyValueError(msg)
    _validate_runner(runner)
    reentry_queue = _runner_reentry_queue(graph, runner)
    generation = [0]

    def run(ctx: _k.Ctx) -> None:
        values = _gate(ctx)
        if values is None:
            return
        generation[0] += 1
        mine = generation[0]

        def body(
            gate: _AsyncCompletionGate | _QueuedCompletionGate, job: _AsyncJob
        ) -> AsyncJobFactory:
            def is_current() -> bool:
                return mine == generation[0] and job.can_apply_completion()

            async def go() -> None:
                try:
                    if not job.active:
                        return
                    awaitable = callback(*values)
                    if not isawaitable(awaitable):
                        msg = "async_node callback must return an awaitable"
                        raise GraphReflyRuntimeError(msg)
                    value = await awaitable
                    gate.emit(value, is_current, final=True)
                except BaseException as error:
                    if _should_suppress_async_error(job, error):
                        return
                    if isinstance(error, Exception):
                        gate.error(error, is_current)
                    else:
                        gate.fatal(error, is_current)
                finally:
                    job.finish()

            return go

        stop = _start_async(graph, ctx, runner, reentry_queue, body)
        if stop is not None:
            ctx.signal.on_abort(stop)

    return cast("Node[T]", graph._add_node(name, dep_list, run, callback, None))


def asyncio_runner(loop: object | None = None) -> AsyncRunner:
    """Return a convenience runner for a caller-owned asyncio event loop."""

    return _AsyncioRunner(loop)


def trio_runner(nursery: object) -> AsyncRunner:
    """Return a convenience runner for a caller-owned Trio nursery."""

    trio = import_module("trio")
    cancel_scope = getattr(trio, "CancelScope", None)
    if not callable(cancel_scope):
        msg = "trio.CancelScope is required for trio_runner"
        raise GraphReflyRuntimeError(msg)
    return _StartSoonRunner(
        nursery,
        cancel_scope_factory=cast("_CancelScopeFactory", cancel_scope),
        owner_name="Trio nursery",
    )


def anyio_runner(task_group: object) -> AsyncRunner:
    """Return a convenience runner for a caller-owned AnyIO task group."""

    anyio = import_module("anyio")
    cancel_scope = getattr(anyio, "CancelScope", None)
    if not callable(cancel_scope):
        msg = "anyio.CancelScope is required for anyio_runner"
        raise GraphReflyRuntimeError(msg)
    return _StartSoonRunner(
        task_group,
        cancel_scope_factory=cast("_CancelScopeFactory", cancel_scope),
        owner_name="AnyIO task group",
    )


def _validate_async_source_args(
    graph: Graph,
    runner: AsyncRunner,
    factory: object,
) -> None:
    graph._check_thread()
    _validate_runner(runner)
    _reject_async_factory_instance(factory)
    if not callable(factory):
        msg = "async source factory must be callable"
        raise GraphReflyValueError(msg)


def _validate_runner(runner: AsyncRunner) -> None:
    spawn = getattr(runner, "spawn", None)
    if not callable(spawn):
        msg = "AsyncRunner must provide spawn(job)"
        raise GraphReflyValueError(msg)


def _runner_reentry_queue(graph: Graph, runner: AsyncRunner) -> GraphReentryQueue | None:
    if isinstance(runner, _GraphReentryRunner):
        if runner._queue._lifetime is not graph._lifetime:
            msg = "wrapped AsyncRunner reentry queue must belong to the target Graph"
            raise GraphReflyRuntimeError(msg)
        if runner._queue.closed:
            msg = "wrapped AsyncRunner reentry queue is closed"
            raise GraphReflyRuntimeError(msg)
        return runner._queue
    return None


def _reject_async_factory_instance(factory: object) -> None:
    if isawaitable(factory) or hasattr(factory, "__aiter__"):
        msg = "async inputs must be factory callables so each activation creates fresh work"
        raise GraphReflyValueError(msg)


def _spawn_runner_job(runner: AsyncRunner, body: AsyncJobFactory) -> object:
    task = runner.spawn(body)
    if iscoroutine(task):
        close = getattr(task, "close", None)
        if callable(close):
            close()
        msg = "AsyncRunner.spawn(job) must not return a raw coroutine"
        raise GraphReflyRuntimeError(msg)
    return task


def _cancel_runner_task(runner: AsyncRunner, task: object | None) -> None:
    try:
        runner_cancel = getattr(runner, "cancel", None)
        if callable(runner_cancel):
            result = runner_cancel(task)
        else:
            task_cancel = getattr(task, "cancel", None)
            if not callable(task_cancel):
                return
            result = task_cancel()
        if isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
    except Exception:
        return


async def _close_async_stream(stream: object) -> None:
    close = getattr(stream, "aclose", None)
    if callable(close):
        try:
            result = close()
            if isawaitable(result):
                await result
        except Exception:
            return
        return
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            result = close()
            if isawaitable(result):
                await result
        except Exception:
            return


def _is_cancellation_error(error: BaseException) -> bool:
    if isinstance(error, BaseExceptionGroup):
        return bool(error.exceptions) and all(
            _is_cancellation_error(item) for item in error.exceptions
        )
    return "Cancel" in type(error).__name__


def _format_async_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"
