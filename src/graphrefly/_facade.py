"""Typed Python facade over the private PyO3 foundation."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from dataclasses import dataclass
from importlib import import_module, metadata
from importlib.metadata import PackageNotFoundError
from inspect import isawaitable, iscoroutine, iscoroutinefunction
from threading import Lock, get_ident
from typing import Any, ClassVar, Final, Literal, NoReturn, Protocol, TypeVar, cast, overload
from weakref import ReferenceType, ref

from graphrefly import _native
from graphrefly.exceptions import (
    CallbackError,
    GraphCallbackError,
    GraphReflyCheckpointError,
    GraphReflyNoDataError,
    GraphReflyRestoreError,
    GraphReflyRuntimeError,
    GraphReflyValueError,
    SubscriberCallbackError,
)

T = TypeVar("T")
U = TypeVar("U")
_VERSION = "0.21.0a0"
_MAX_CALLBACK_ERRORS = 32
_NO_DEFAULT = object()
_GRAPH_REENTRY_QUEUE_TOKEN = object()


class Sentinel:
    """Python representation of protocol SENTINEL in raw ctx wave data."""

    __slots__ = ()
    _instance: ClassVar[Sentinel | None] = None

    def __new__(cls) -> Sentinel:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "graphrefly.SENTINEL"


SENTINEL: Final = Sentinel()


@dataclass(frozen=True, slots=True)
class DataMessage[T]:
    """DATA observation with a domain payload.

    `None` is a valid domain payload in Python v1; no-DATA is represented by
    message shape, not by the payload value.
    """

    value: T
    kind: Literal["DATA"] = "DATA"


@dataclass(frozen=True, slots=True)
class ErrorMessage:
    """ERROR observation with a Python-owned error envelope."""

    error: GraphCallbackError
    kind: Literal["ERROR"] = "ERROR"


@dataclass(frozen=True, slots=True)
class ControlMessage:
    """Non-DATA / non-ERROR observation."""

    kind: str
    value: object | None = None


type Message[T] = DataMessage[T] | ErrorMessage | ControlMessage
type ObserverErrorHandler = Callable[[SubscriberCallbackError], object]
type PausableMode = bool | Literal["resumeAll"]
type NodeCallback = Callable[["Ctx"], object]
type AsyncJobFactory = Callable[[], Awaitable[None]]
type _CompletionPredicate = Callable[[], bool]
type _CancelScopeFactory = Callable[[], object]
type GraphCheckpoint = dict[str, Any]
type RestoreRegistry = Any


@dataclass(frozen=True, slots=True)
class WireBridgeStatus:
    state: Literal["idle", "started", "open", "waiting", "closed", "errored", "exhausted"]
    session_id: str
    cursor: int = 0
    next_seq: int = 0
    pending: int = 0
    attempts: int = 0
    acked: int = 0
    nacked: int = 0
    errors: int = 0
    last_seq: int | None = None
    last_delay_ms: int | None = None


@dataclass(frozen=True, slots=True)
class WireBridgeIssue:
    code: Literal["bridge_error", "invalid"]
    message: str


@dataclass(frozen=True, slots=True)
class WireEdgeGroupIssue:
    code: Literal[
        "missing_snapshot",
        "unknown_edge",
        "duplicate_dirty",
        "duplicate_data",
        "data_before_dirty",
        "competing_cause",
        "malformed_frame",
        "incomplete_cause",
    ]
    message: str
    edge_id: str | None = None
    cause_id: str | None = None
    active_cause_id: str | None = None


@dataclass(frozen=True, slots=True)
class WireEdgeGroupStatus:
    state: Literal["idle", "collecting", "released", "issues"]
    expected_edges: tuple[str, ...]
    active_cause_id: str | None = None
    dirty: int = 0
    data: int = 0
    released: int = 0
    issues: int = 0
    last_issue: WireEdgeGroupIssue | None = None


@dataclass(frozen=True, slots=True)
class WireBridgeProtobufStatus:
    direction: Literal["inbound", "outbound"]
    state: Literal["valid", "invalid"]


@dataclass(frozen=True, slots=True)
class WireBridgeProtobufIssue:
    direction: Literal["inbound", "outbound"]
    category: str
    message: str


@dataclass(frozen=True, slots=True)
class WireBridgeAckTimeout:
    seq: int
    attempt: int
    observed_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class WireBridgeAckDriverStatus:
    state: Literal["idle", "ready", "issues"]
    timeout_ms: int
    last_timeout: WireBridgeAckTimeout | None = None


@dataclass(frozen=True, slots=True)
class WireBridgeAckDriverIssue:
    code: Literal["invalid_clock", "invalid_timeout"]
    message: str


class AsyncRunner(Protocol):
    """Framework-neutral async job runner supplied explicitly by the host."""

    def spawn(self, job: AsyncJobFactory) -> object: ...


@dataclass(frozen=True, slots=True)
class RestoreRef:
    """Factory reference metadata recorded into a strict-JSON checkpoint."""

    ref: str
    config: object | None = None
    config_version: object | None = None


class RestoreContext(Protocol):
    """Descriptor construction context with no raw Node/Ctx/native handles."""

    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str | None: ...

    @property
    def deps(self) -> list[str]: ...

    @property
    def config(self) -> object | None: ...

    @property
    def config_version(self) -> object | None: ...

    @property
    def checkpoint(self) -> GraphCheckpoint: ...

    def register_state(self) -> None: ...

    def register_node(
        self,
        callback: Callable[[Ctx], object],
        factory: str | None = None,
    ) -> None: ...


class RestoreDescriptor(Protocol):
    """A factory-name restore descriptor used by `restore_graph`."""

    ref: str

    def create(self, ctx: RestoreContext) -> object: ...


class _BoundRestoreContext:
    def __init__(
        self,
        native: _native.RestoreContext,
        *,
        owner_thread: int,
        lifetime: _GraphLifetime,
    ) -> None:
        self._native = native
        self._owner_thread = owner_thread
        self._lifetime = lifetime

    @property
    def id(self) -> str:
        return self._native.id

    @property
    def name(self) -> str | None:
        return self._native.name

    @property
    def deps(self) -> list[str]:
        return self._native.deps

    @property
    def config(self) -> object | None:
        return self._native.config

    @property
    def config_version(self) -> object | None:
        return self._native.config_version

    @property
    def checkpoint(self) -> GraphCheckpoint:
        return self._native.checkpoint

    def register_state(self) -> None:
        try:
            self._native.register_state()
        except RuntimeError as error:
            raise GraphReflyRestoreError(str(error)) from error

    def register_node(
        self,
        callback: Callable[[Ctx], object],
        factory: str | None = None,
    ) -> None:
        _reject_async_callable(callback)
        owner_thread = self._owner_thread
        lifetime = self._lifetime

        def native_callback(native_ctx: _native.Ctx) -> None:
            value = callback(
                Ctx(
                    native_ctx,
                    owner_thread=owner_thread,
                    lifetime=lifetime,
                )
            )
            _reject_awaitable(value)

        try:
            self._native.register_node(native_callback, factory)
        except RuntimeError as error:
            raise GraphReflyRestoreError(str(error)) from error


class _BoundRestoreDescriptor:
    def __init__(
        self,
        descriptor: RestoreDescriptor,
        *,
        owner_thread: int,
        lifetime: _GraphLifetime,
    ) -> None:
        self._descriptor = descriptor
        self._owner_thread = owner_thread
        self._lifetime = lifetime
        self.ref = descriptor.ref

    def create(self, native_ctx: _native.RestoreContext) -> object:
        return self._descriptor.create(
            _BoundRestoreContext(
                native_ctx,
                owner_thread=self._owner_thread,
                lifetime=self._lifetime,
            )
        )


class _RestoreRegistryFacade:
    def __init__(
        self,
        entries: list[RestoreDescriptor],
        *,
        include_builtins: bool,
    ) -> None:
        self._entries = entries
        self._include_builtins = include_builtins

    def _native_registry(
        self,
        *,
        owner_thread: int,
        lifetime: _GraphLifetime,
    ) -> _native.RestoreRegistry:
        entries = [
            _BoundRestoreDescriptor(
                entry,
                owner_thread=owner_thread,
                lifetime=lifetime,
            )
            for entry in self._entries
        ]
        return _native.restore_registry(cast("list[object]", entries), self._include_builtins)


def _validate_restore_registry_entries(
    entries: list[RestoreDescriptor],
    *,
    include_builtins: bool,
) -> None:
    seen = {"state", "map"} if include_builtins else set()
    for entry in entries:
        ref = getattr(entry, "ref", None)
        if not isinstance(ref, str) or ref == "":
            msg = "restore registry entries must expose a non-empty string 'ref'"
            raise GraphReflyValueError(msg)
        if ref in seen:
            msg = f"duplicate restore registry ref '{ref}'"
            raise GraphReflyValueError(msg)
        seen.add(ref)


class _GraphLifetime:
    def __init__(self, owner_thread: int) -> None:
        self.owner_thread = owner_thread
        self._lock = Lock()
        self.closed = False
        self.poisoned = False
        self.subscriptions: list[ReferenceType[Subscription]] = []
        self.async_jobs: list[ReferenceType[_AsyncJob]] = []
        self.reentry_queues: list[ReferenceType[GraphReentryQueue]] = []
        self.resources: list[ReferenceType[object]] = []

    @property
    def closed_message(self) -> str:
        if self.poisoned:
            return "GraphReFly graph is closed after a fatal host boundary abort"
        return "GraphReFly graph is closed"

    def register(self, subscription: Subscription) -> None:
        should_close = False
        with self._lock:
            if self.closed:
                should_close = True
            else:
                self.subscriptions.append(ref(subscription))
        if should_close:
            subscription._close_from_graph()
            raise GraphReflyRuntimeError(self.closed_message)

    def unregister(self, subscription: Subscription) -> None:
        with self._lock:
            self.subscriptions = [
                item
                for item in self.subscriptions
                if (live := item()) is not None and live is not subscription
            ]

    def register_async_job(self, job: _AsyncJob) -> None:
        cancel = False
        with self._lock:
            if self.closed:
                cancel = True
            else:
                self.async_jobs.append(ref(job))
        if cancel:
            job.cancel()

    def unregister_async_job(self, job: _AsyncJob) -> None:
        with self._lock:
            self.async_jobs = [
                item
                for item in self.async_jobs
                if (live := item()) is not None and live is not job
            ]

    def register_reentry_queue(self, queue: GraphReentryQueue) -> None:
        with self._lock:
            if self.closed:
                queue._close_from_graph()
                raise GraphReflyRuntimeError(self.closed_message)
            self.reentry_queues.append(ref(queue))

    def unregister_reentry_queue(self, queue: GraphReentryQueue) -> None:
        with self._lock:
            self.reentry_queues = [
                item
                for item in self.reentry_queues
                if (live := item()) is not None and live is not queue
            ]

    def register_resource(self, resource: object) -> None:
        with self._lock:
            if self.closed:
                close = getattr(resource, "_close_from_graph", None)
                if callable(close):
                    close()
                raise GraphReflyRuntimeError(self.closed_message)
            self.resources.append(ref(resource))

    def unregister_resource(self, resource: object) -> None:
        with self._lock:
            self.resources = [
                item
                for item in self.resources
                if (live := item()) is not None and live is not resource
            ]

    def close(self, *, suppress_errors: bool = False) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            resources = tuple(item() for item in reversed(self.resources))
            queues = tuple(item() for item in self.reentry_queues)
            jobs = tuple(item() for item in self.async_jobs)
            subscriptions = tuple(item() for item in self.subscriptions)
            self.resources.clear()
            self.reentry_queues.clear()
            self.async_jobs.clear()
            self.subscriptions.clear()
        for resource in resources:
            if resource is not None:
                close = getattr(resource, "_close_from_graph", None)
                if callable(close):
                    close()
        self._close_reentry_queues(queues)
        self._cancel_async_jobs(jobs)
        first_error: BaseException | None = None
        for subscription in subscriptions:
            if subscription is not None:
                try:
                    subscription._close_from_graph()
                except BaseException as error:
                    if first_error is None:
                        first_error = error
        if first_error is not None and not suppress_errors:
            raise first_error

    def poison(self) -> None:
        with self._lock:
            self.poisoned = True
        self.close(suppress_errors=True)

    def is_closed(self) -> bool:
        with self._lock:
            return self.closed

    def _cancel_async_jobs(self, jobs: tuple[_AsyncJob | None, ...]) -> None:
        for job in jobs:
            if job is not None:
                job.cancel()

    def _close_reentry_queues(self, queues: tuple[GraphReentryQueue | None, ...]) -> None:
        for queue in queues:
            if queue is not None:
                queue._close_from_graph()


@dataclass(frozen=True, slots=True)
class GraphEvent:
    """Graph-level observation from the native engine."""

    path: str
    message: Message[Any]
    tier: int
    seq: int


@dataclass(frozen=True, slots=True)
class PullContext:
    """Read-only PULL demand context visible during a pull-holder invocation."""

    pull_id: str
    params: object | None = None


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
        with self._lock:
            return self._closed

    @property
    def pending_count(self) -> int:
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


class Subscription:
    """Idempotent subscription handle."""

    def __init__(
        self,
        native: _native.Subscription,
        *,
        owner_thread: int,
        lifetime: _GraphLifetime | None = None,
        callback_errors: list[SubscriberCallbackError] | None = None,
    ) -> None:
        self._native = native
        self._owner_thread = owner_thread
        self._lifetime = lifetime
        self._callback_errors = callback_errors if callback_errors is not None else []
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def callback_errors(self) -> tuple[SubscriberCallbackError, ...]:
        return tuple(self._callback_errors)

    def unsubscribe(self) -> None:
        self._check_thread()
        if not self._closed:
            self._unsubscribe_native()
            if self._lifetime is not None:
                self._lifetime.unregister(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.unsubscribe()

    def _check_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python subscriptions are bound to their creating thread in v0"
            raise GraphReflyRuntimeError(msg)

    def _close_from_graph(self) -> None:
        if not self._closed:
            self._unsubscribe_native()

    def _unsubscribe_native(self) -> None:
        try:
            self._native.unsubscribe()
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            self._closed = True
            if self._lifetime is not None:
                _poison_on_fatal(self._lifetime, error)
            raise
        self._closed = True


class _AsyncCompletionGate:
    """Owner-thread gate around the hidden native async completion handle."""

    def __init__(
        self,
        native: Any,
        *,
        owner_thread: int,
        lifetime: _GraphLifetime,
        reentry_queue: GraphReentryQueue | None = None,
    ) -> None:
        self._native = native
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
        return _QueuedCompletionGate(
            queue=self._reentry_queue,
            gate_id=self._reentry_gate_id,
        )

    def register_deactivation(self, callback: Callable[[], object]) -> None:
        def native_callback() -> None:
            self.close()
            callback()

        self._native.on_deactivation(native_callback)

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

    def error(
        self,
        error: BaseException,
        should_apply: _CompletionPredicate | None = None,
    ) -> bool:
        if self._should_queue():
            return self._queue("error", error, should_apply, final=True)
        return self._error_now(error, should_apply, final=True)

    def fatal(
        self,
        error: BaseException,
        should_apply: _CompletionPredicate | None = None,
    ) -> bool:
        if self._should_queue():
            return self._queue("fatal", error, should_apply, final=True)
        return self._fatal_now(error, should_apply, final=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reentry_queue is not None:
            self._reentry_queue._unregister_gate(self._reentry_gate_id)

    def _emit_now(
        self,
        value: object,
        should_apply: _CompletionPredicate | None = None,
        *,
        final: bool = False,
    ) -> bool:
        try:
            if not self._can_reenter(should_apply):
                return False
            _reject_awaitable(value)
            _reject_sentinel_data(value)
            try:
                self._native.emit(value)
            except RuntimeError as error:
                raise GraphReflyRuntimeError(str(error)) from error
            except BaseException as error:
                _poison_on_fatal(self._lifetime, error)
            return True
        finally:
            if final:
                self.close()

    def _complete_now(
        self,
        should_apply: _CompletionPredicate | None = None,
        *,
        final: bool = True,
    ) -> bool:
        try:
            if not self._can_reenter(should_apply):
                return False
            try:
                self._native.complete()
            except RuntimeError as error:
                raise GraphReflyRuntimeError(str(error)) from error
            except BaseException as error:
                _poison_on_fatal(self._lifetime, error)
            return True
        finally:
            if final:
                self.close()

    def _resolve_now(
        self,
        value: object,
        should_apply: _CompletionPredicate | None = None,
        *,
        final: bool = True,
    ) -> bool:
        try:
            if not self._can_reenter(should_apply):
                return False
            _reject_awaitable(value)
            _reject_sentinel_data(value)
            try:
                self._native.resolve(value)
            except RuntimeError as error:
                raise GraphReflyRuntimeError(str(error)) from error
            except BaseException as error:
                _poison_on_fatal(self._lifetime, error)
            return True
        finally:
            if final:
                self.close()

    def _error_now(
        self,
        error: BaseException,
        should_apply: _CompletionPredicate | None = None,
        *,
        final: bool = True,
    ) -> bool:
        try:
            if not self._can_reenter(should_apply):
                return False
            try:
                self._native.error(_format_async_error(error))
            except RuntimeError as native_error:
                raise GraphReflyRuntimeError(str(native_error)) from native_error
            except BaseException as native_error:
                _poison_on_fatal(self._lifetime, native_error)
            return True
        finally:
            if final:
                self.close()

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
        if self._closed:
            return False
        if self._lifetime.is_closed():
            return False
        if should_apply is not None and not should_apply():
            return False
        try:
            return bool(self._native.is_live())
        except RuntimeError:
            return False
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

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
            return (
                not self._done
                and not self._cancel_requested
                and not self._lifetime.is_closed()
            )

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
            should_cancel = (
                self._cancel_requested or self._done or self._lifetime.is_closed()
            )
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


class Ctx:
    """Python-owned facade for an advanced node callback invocation."""

    def __init__(
        self,
        native: _native.Ctx,
        *,
        owner_thread: int,
        lifetime: _GraphLifetime,
    ) -> None:
        self._native = native
        self._owner_thread = owner_thread
        self._lifetime = lifetime

    @property
    def dep_len(self) -> int:
        self._check_thread()
        try:
            return self._native.dep_len()
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def has_data(self, index: int) -> bool:
        self._check_thread()
        try:
            has_value, _value = self._native.data_entry(index)
        except IndexError:
            raise
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)
        return has_value

    @overload
    def data(self, index: int) -> object: ...

    @overload
    def data(self, index: int, default: U) -> object | U: ...

    def data(self, index: int, default: object = _NO_DEFAULT) -> object:
        self._check_thread()
        try:
            has_value, value = self._native.data_entry(index)
        except IndexError:
            raise
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)
        if not has_value:
            if default is _NO_DEFAULT:
                msg = f"dependency {index} has no DATA value in this ctx invocation"
                raise GraphReflyNoDataError(msg)
            return default
        return value

    @property
    def wave_data(self) -> list[list[list[object]]]:
        self._check_thread()
        try:
            return self._native.wave_data(SENTINEL)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def terminal(self, index: int) -> bool | object:
        self._check_thread()
        try:
            return self._native.terminal(index)
        except IndexError:
            raise
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    @property
    def pull(self) -> PullContext | None:
        self._check_thread()
        try:
            native_pull = self._native._pull_context()
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)
        if native_pull is None:
            return None
        pull_id, params = native_pull
        return PullContext(pull_id=pull_id, params=params)

    @overload
    def pull_params(self) -> object | None: ...

    @overload
    def pull_params(self, default: U) -> object | U: ...

    def pull_params(self, default: object = None) -> object:
        pull = self.pull
        if pull is None or pull.params is None:
            return default
        return pull.params

    @property
    def rewire_next(self) -> RewireNext:
        self._check_thread()
        return RewireNext(self)

    def emit(self, value: object) -> None:
        self._check_thread()
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        try:
            self._native.emit(value)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    @property
    def has_state(self) -> bool:
        self._check_thread()
        try:
            has_value, _value = self._native.state_entry()
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)
        return has_value

    @property
    def state(self) -> object | None:
        self._check_thread()
        try:
            _has_value, value = self._native.state_entry()
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)
        return value

    @state.setter
    def state(self, value: object) -> None:
        self._check_thread()
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        try:
            self._native.set_state(value)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def persist_state(self, on: bool = True) -> None:
        self._check_thread()
        try:
            self._native.state_persist(on)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def on_invalidate(self, callback: Callable[[], object]) -> None:
        self._check_thread()
        _reject_async_callable(callback)

        def native_callback() -> None:
            result = callback()
            _reject_awaitable(result)

        try:
            self._native.on_invalidate(native_callback)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def on_deactivation(self, callback: Callable[[], object]) -> None:
        self._check_thread()
        _reject_async_callable(callback)

        def native_callback() -> None:
            result = callback()
            _reject_awaitable(result)

        try:
            self._native.on_deactivation(native_callback)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def request_pull(
        self,
        pull_id: str,
        params: object | None = None,
        *,
        toward_dep: int | None = None,
    ) -> None:
        self._check_thread()
        _reject_non_string_pull_id(pull_id)
        _reject_awaitable(params)
        _reject_sentinel_data(params)
        toward_dep = _normalize_toward_dep(toward_dep)
        self._reject_unknown_toward_dep(toward_dep)
        try:
            self._native._up_pull(pull_id, params, toward_dep)
        except IndexError:
            raise
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def request_pull_next(
        self,
        pull_id: str,
        params: object | None = None,
        *,
        toward_dep: int | None = None,
    ) -> None:
        self._check_thread()
        _reject_non_string_pull_id(pull_id)
        _reject_awaitable(params)
        _reject_sentinel_data(params)
        toward_dep = _normalize_toward_dep(toward_dep)
        self._reject_unknown_toward_dep(toward_dep)
        try:
            self._native._up_next_pull(pull_id, params, toward_dep)
        except IndexError:
            raise
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def _check_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python ctx objects are bound to their creating thread in v0"
            raise GraphReflyRuntimeError(msg)
        if self._lifetime.closed:
            raise GraphReflyRuntimeError(self._lifetime.closed_message)

    def _reject_unknown_toward_dep(self, toward_dep: int | None) -> None:
        if toward_dep is not None and toward_dep >= self.dep_len:
            msg = "toward_dep must reference an existing dependency in the Python facade"
            raise GraphReflyValueError(msg)


class RewireNext:
    """Callback-scoped deferred topology mutation facade."""

    def __init__(self, ctx: Ctx) -> None:
        self._ctx = ctx

    def subscribe_dep(self, dep: Node[Any], callback: NodeCallback) -> None:
        self._ctx._check_thread()
        callback = _validate_rewire_callback(callback)
        native_dep = self._native_node(dep)
        native_callback = self._native_callback(callback)
        try:
            self._ctx._native._rewire_next_subscribe_dep(native_dep, native_callback)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._ctx._lifetime, error)

    def unsubscribe_dep(self, dep: Node[Any], callback: NodeCallback) -> None:
        self._ctx._check_thread()
        callback = _validate_rewire_callback(callback)
        native_dep = self._native_node(dep)
        native_callback = self._native_callback(callback)
        try:
            self._ctx._native._rewire_next_unsubscribe_dep(native_dep, native_callback)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._ctx._lifetime, error)

    def replace_deps(self, deps: Iterable[Node[Any]], callback: NodeCallback) -> None:
        self._ctx._check_thread()
        callback = _validate_rewire_callback(callback)
        native_deps = [self._native_node(dep) for dep in deps]
        native_callback = self._native_callback(callback)
        try:
            self._ctx._native._rewire_next_replace_deps(native_deps, native_callback)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._ctx._lifetime, error)

    def _native_node(self, dep: Node[Any]) -> _native.Node:
        if not isinstance(dep, Node):
            msg = "rewire_next deps must be graphrefly.Node objects"
            raise GraphReflyValueError(msg)
        dep._check_thread()
        if dep._lifetime is not self._ctx._lifetime:
            msg = "rewire_next deps must belong to the same GraphReFly graph"
            raise GraphReflyRuntimeError(msg)
        return dep._native

    def _native_callback(self, callback: NodeCallback) -> Callable[[_native.Ctx], None]:
        owner_thread = self._ctx._owner_thread
        lifetime = self._ctx._lifetime

        def native_callback(native_ctx: _native.Ctx) -> None:
            value = callback(
                Ctx(
                    native_ctx,
                    owner_thread=owner_thread,
                    lifetime=lifetime,
                )
            )
            _reject_awaitable(value)

        return native_callback


class Node[T]:
    """A typed Python handle around an opaque native node."""

    def __init__(
        self,
        native: _native.Node,
        *,
        owner_thread: int,
        lifetime: _GraphLifetime,
        writable: bool = False,
    ) -> None:
        self._native = native
        self._owner_thread = owner_thread
        self._lifetime = lifetime
        self._writable = writable

    def set(self, value: T) -> None:
        self._check_thread()
        if not self._writable:
            msg = "set() is only available on state nodes in the v0 Python facade"
            raise GraphReflyRuntimeError(msg)
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        try:
            self._native.set(value)
        except ValueError as error:
            raise GraphReflyValueError(str(error)) from error
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    @overload
    def cache(self) -> T: ...

    @overload
    def cache(self, default: U) -> T | U: ...

    def cache(self, default: object = _NO_DEFAULT) -> object:
        self._check_thread()
        try:
            has_value, value = self._native.cache_entry()
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)
        if not has_value:
            if default is _NO_DEFAULT:
                msg = "node has no cached DATA value"
                raise GraphReflyNoDataError(msg)
            return default
        return cast("T", value)

    @property
    def has_value(self) -> bool:
        self._check_thread()
        try:
            has_value, _value = self._native.cache_entry()
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)
        return has_value

    @property
    def status(self) -> str:
        self._check_thread()
        try:
            return self._native.status()
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def subscribe(
        self,
        callback: Callable[[Message[T]], object],
        *,
        on_error: ObserverErrorHandler | None = None,
    ) -> Subscription:
        self._check_thread()
        _reject_async_callable(callback)
        if on_error is not None:
            _reject_async_callable(on_error)
        callback_errors: list[SubscriberCallbackError] = []

        def native_callback(kind: str, value: object) -> None:
            try:
                result = callback(_message_from_native(kind, value))
                _reject_awaitable(result)
            except Exception as error:
                _record_observer_error(error, callback_errors, on_error)

        try:
            subscription = Subscription(
                self._native.subscribe(native_callback),
                owner_thread=self._owner_thread,
                lifetime=self._lifetime,
                callback_errors=callback_errors,
            )
            self._lifetime.register(subscription)
            return subscription
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def _check_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python nodes are bound to their creating thread in v0"
            raise GraphReflyRuntimeError(msg)
        if self._lifetime.closed:
            raise GraphReflyRuntimeError(self._lifetime.closed_message)


class _MappedNode(Node[T]):
    def __init__(
        self,
        native: _native.Node,
        *,
        owner_thread: int,
        lifetime: _GraphLifetime,
        mapper: Callable[[object], T],
    ) -> None:
        super().__init__(native, owner_thread=owner_thread, lifetime=lifetime)
        self._mapper = mapper

    @overload
    def cache(self) -> T: ...

    @overload
    def cache(self, default: U) -> T | U: ...

    def cache(self, default: object = _NO_DEFAULT) -> object:
        value = super().cache(default)
        if value is default:
            return value
        return self._mapper(value)

    def subscribe(
        self,
        callback: Callable[[Message[T]], object],
        *,
        on_error: ObserverErrorHandler | None = None,
    ) -> Subscription:
        def mapped(message: Message[object]) -> object:
            if isinstance(message, DataMessage):
                return callback(DataMessage(self._mapper(message.value)))
            return callback(cast("Message[T]", message))

        return super().subscribe(mapped, on_error=on_error)


def _wire_bridge_status(value: object) -> WireBridgeStatus:
    data = cast("dict[str, Any]", value)
    return WireBridgeStatus(
        state=cast("Any", data["state"]),
        session_id=str(data["session_id"]),
        cursor=int(data.get("cursor", 0)),
        next_seq=int(data.get("next_seq", 0)),
        pending=int(data.get("pending", 0)),
        attempts=int(data.get("attempts", 0)),
        acked=int(data.get("acked", 0)),
        nacked=int(data.get("nacked", 0)),
        errors=int(data.get("errors", 0)),
        last_seq=cast("int | None", data.get("last_seq")),
        last_delay_ms=cast("int | None", data.get("last_delay_ms")),
    )


def _wire_bridge_issue(value: object) -> WireBridgeIssue:
    data = cast("dict[str, Any]", value)
    return WireBridgeIssue(
        code=cast("Any", data.get("code", "invalid")),
        message=str(data.get("message", "")),
    )


def _wire_edge_group_issue(value: object) -> WireEdgeGroupIssue:
    data = cast("dict[str, Any]", value)
    return WireEdgeGroupIssue(
        code=cast("Any", data["code"]),
        message=str(data["message"]),
        edge_id=cast("str | None", data.get("edge_id")),
        cause_id=cast("str | None", data.get("cause_id")),
        active_cause_id=cast("str | None", data.get("active_cause_id")),
    )


def _wire_edge_group_status(value: object) -> WireEdgeGroupStatus:
    data = cast("dict[str, Any]", value)
    last_issue = data.get("last_issue")
    return WireEdgeGroupStatus(
        state=cast("Any", data["state"]),
        expected_edges=tuple(cast("Iterable[str]", data.get("expected_edges", ()))),
        active_cause_id=cast("str | None", data.get("active_cause_id")),
        dirty=int(data.get("dirty", 0)),
        data=int(data.get("data", 0)),
        released=int(data.get("released", 0)),
        issues=int(data.get("issues", 0)),
        last_issue=_wire_edge_group_issue(last_issue) if isinstance(last_issue, dict) else None,
    )


def _wire_bridge_protobuf_status(value: object) -> WireBridgeProtobufStatus:
    data = cast("dict[str, Any]", value)
    return WireBridgeProtobufStatus(
        direction=cast("Any", data["direction"]),
        state=cast("Any", data["state"]),
    )


def _wire_bridge_protobuf_issue(value: object) -> WireBridgeProtobufIssue:
    data = cast("dict[str, Any]", value)
    return WireBridgeProtobufIssue(
        direction=cast("Any", data["direction"]),
        category=str(data["category"]),
        message=str(data["message"]),
    )


def _wire_bridge_ack_timeout(value: object) -> WireBridgeAckTimeout:
    data = cast("dict[str, Any]", value)
    return WireBridgeAckTimeout(
        seq=int(data.get("seq", 0)),
        attempt=int(data.get("attempt", 0)),
        observed_at_ms=cast("int | None", data.get("observed_at_ms")),
    )


def _wire_bridge_ack_driver_status(value: object) -> WireBridgeAckDriverStatus:
    data = cast("dict[str, Any]", value)
    last_timeout = data.get("last_timeout")
    return WireBridgeAckDriverStatus(
        state=cast("Any", data.get("state", "idle")),
        timeout_ms=int(data.get("timeout_ms", 0)),
        last_timeout=_wire_bridge_ack_timeout(last_timeout)
        if isinstance(last_timeout, dict)
        else None,
    )


def _wire_bridge_ack_driver_issue(value: object) -> WireBridgeAckDriverIssue:
    data = cast("dict[str, Any]", value)
    return WireBridgeAckDriverIssue(
        code=cast("Any", data.get("code", "invalid_clock")),
        message=str(data.get("message", "")),
    )


class WireBridge:
    def __init__(self, native: _native._WireBridge, graph: Graph) -> None:
        self._native = native
        self._owner_thread = graph._owner_thread
        self._lifetime = graph._lifetime
        self.status: Node[WireBridgeStatus] = _MappedNode(
            native.status,
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
            mapper=_wire_bridge_status,
        )
        self.issues: Node[WireBridgeIssue] = _MappedNode(
            native.issues,
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
            mapper=_wire_bridge_issue,
        )
        self._released = False
        self._lifetime.register_resource(self)

    def __enter__(self) -> WireBridge:
        self._check_thread()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    def release(self) -> None:
        self._check_thread()
        if self._released:
            return
        self._native.release()
        self._released = True
        self._lifetime.unregister_resource(self)

    def _close_from_graph(self) -> None:
        if self._released:
            return
        self._native.release()
        self._released = True

    def _check_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python wire bridge facades are bound to their creating thread"
            raise GraphReflyRuntimeError(msg)
        if self._lifetime.closed and not self._released:
            raise GraphReflyRuntimeError(self._lifetime.closed_message)

    def _check_attachable(self) -> None:
        self._check_thread()
        if self._released:
            raise GraphReflyRuntimeError("wire bridge facade is released")


class WireBridgeProtobuf:
    def __init__(self, native: _native._WireBridgeProtobuf, graph: Graph) -> None:
        self._native = native
        self._owner_thread = graph._owner_thread
        self._lifetime = graph._lifetime
        self.inbound_bytes: Node[bytes] = Node(
            native.inbound_bytes,
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
            writable=True,
        )
        self.outbound_bytes: Node[bytes] = Node(
            native.outbound_bytes,
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
        )
        self.status: Node[WireBridgeProtobufStatus] = _MappedNode(
            native.status,
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
            mapper=_wire_bridge_protobuf_status,
        )
        self.issues: Node[WireBridgeProtobufIssue] = _MappedNode(
            native.issues,
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
            mapper=_wire_bridge_protobuf_issue,
        )
        self._released = False
        self._lifetime.register_resource(self)

    def __enter__(self) -> WireBridgeProtobuf:
        self._check_thread()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    def release(self) -> None:
        self._check_thread()
        if self._released:
            return
        self._native.release()
        self._released = True
        self._lifetime.unregister_resource(self)

    def _close_from_graph(self) -> None:
        if self._released:
            return
        self._native.release()
        self._released = True

    def _check_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python protobuf facades are owner-thread bound"
            raise GraphReflyRuntimeError(msg)
        if self._lifetime.closed and not self._released:
            raise GraphReflyRuntimeError(self._lifetime.closed_message)


class WireEdgeGroup:
    def __init__(self, native: _native._WireEdgeGroup, graph: Graph) -> None:
        self._native = native
        self._owner_thread = graph._owner_thread
        self._lifetime = graph._lifetime
        self.inbound_edges: dict[str, Node[bytes]] = {
            str(edge_id): Node(
                native_node,
                owner_thread=self._owner_thread,
                lifetime=self._lifetime,
            )
            for edge_id, native_node in dict(native.inbound_edges).items()
        }
        self.status: Node[WireEdgeGroupStatus] = _MappedNode(
            native.status,
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
            mapper=_wire_edge_group_status,
        )
        self.issues: Node[WireEdgeGroupIssue] = _MappedNode(
            native.issues,
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
            mapper=_wire_edge_group_issue,
        )
        self._released = False
        self._lifetime.register_resource(self)

    def __enter__(self) -> WireEdgeGroup:
        self._check_thread()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    def release(self) -> None:
        self._check_thread()
        if self._released:
            return
        self._native.release()
        self._released = True
        self._lifetime.unregister_resource(self)

    def _close_from_graph(self) -> None:
        if self._released:
            return
        self._native.release()
        self._released = True

    def _check_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python wire edge groups are owner-thread bound"
            raise GraphReflyRuntimeError(msg)
        if self._lifetime.closed and not self._released:
            raise GraphReflyRuntimeError(self._lifetime.closed_message)


class WireBridgeAckDriver:
    def __init__(self, native: _native._WireBridgeAckDriver, graph: Graph) -> None:
        self._native = native
        self._owner_thread = graph._owner_thread
        self._lifetime = graph._lifetime
        self.timeouts: Node[WireBridgeAckTimeout] = _MappedNode(
            native.timeouts,
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
            mapper=_wire_bridge_ack_timeout,
        )
        self.status: Node[WireBridgeAckDriverStatus] = _MappedNode(
            native.status,
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
            mapper=_wire_bridge_ack_driver_status,
        )
        self.issues: Node[WireBridgeAckDriverIssue] = _MappedNode(
            native.issues,
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
            mapper=_wire_bridge_ack_driver_issue,
        )
        self._released = False
        self._lifetime.register_resource(self)

    def __enter__(self) -> WireBridgeAckDriver:
        self._check_thread()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    def release(self) -> None:
        self._check_thread()
        if self._released:
            return
        self._native.release()
        self._released = True
        self._lifetime.unregister_resource(self)

    def _close_from_graph(self) -> None:
        if self._released:
            return
        self._native.release()
        self._released = True

    def _check_thread(self) -> None:
        if get_ident() != self._owner_thread:
            raise GraphReflyRuntimeError("GraphReFly Python ack drivers are owner-thread bound")
        if self._lifetime.closed and not self._released:
            raise GraphReflyRuntimeError(self._lifetime.closed_message)


class Graph:
    """Graph-first authoring facade over the native Rust engine."""

    def __init__(self, name: str | None = None) -> None:
        self._owner_thread = get_ident()
        self._lifetime = _GraphLifetime(self._owner_thread)
        self._native = _native.Graph(name)
        self._reentry_queue: GraphReentryQueue | None = None

    @classmethod
    def _from_native(
        cls,
        native: _native.Graph,
        *,
        owner_thread: int | None = None,
        lifetime: _GraphLifetime | None = None,
    ) -> Graph:
        graph = cls.__new__(cls)
        graph._owner_thread = get_ident() if owner_thread is None else owner_thread
        graph._lifetime = (
            _GraphLifetime(graph._owner_thread) if lifetime is None else lifetime
        )
        graph._native = native
        graph._reentry_queue = None
        return graph

    def __enter__(self) -> Graph:
        self._check_thread()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        self._check_thread(allow_closed=True)
        return self._lifetime.is_closed()

    def close(self) -> None:
        self._check_thread(allow_closed=True)
        try:
            self._native.raise_pending_fatal()
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)
        finally:
            self._lifetime.close()

    def checkpoint(self) -> GraphCheckpoint:
        self._check_thread()
        try:
            return self._native.checkpoint()
        except ValueError as error:
            raise GraphReflyCheckpointError(str(error)) from error
        except RuntimeError as error:
            raise GraphReflyCheckpointError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def reentry_queue(self) -> GraphReentryQueue:
        """Return this graph's explicit owner-thread async re-entry queue."""

        self._check_thread()
        if self._reentry_queue is None or self._reentry_queue.closed:
            self._reentry_queue = GraphReentryQueue(
                owner_thread=self._owner_thread,
                lifetime=self._lifetime,
                _token=_GRAPH_REENTRY_QUEUE_TOKEN,
            )
        return self._reentry_queue

    def state(self, value: T, name: str | None = None) -> Node[T]:
        self._check_thread()
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        try:
            return Node(
                self._native.state(value, name),
                owner_thread=self._owner_thread,
                lifetime=self._lifetime,
                writable=True,
            )
        except ValueError as error:
            raise GraphReflyValueError(str(error)) from error
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    @overload
    def producer(self, callback: Callable[[], T], name: str | None = None) -> Node[T]: ...

    @overload
    def producer(
        self,
        callback: None = None,
        name: str | None = None,
    ) -> Callable[[Callable[[], T]], Node[T]]: ...

    def producer(
        self,
        callback: Callable[[], T] | None = None,
        name: str | None = None,
    ) -> Node[T] | Callable[[Callable[[], T]], Node[T]]:
        self._check_thread()
        if callback is None:
            return lambda fn: self.producer(fn, name or fn.__name__)
        _reject_async_callable(callback)

        def native_callback() -> T:
            value = callback()
            _reject_awaitable(value)
            _reject_sentinel_data(value)
            return value

        try:
            return Node(
                self._native.producer(native_callback, name),
                owner_thread=self._owner_thread,
                lifetime=self._lifetime,
            )
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    @overload
    def derived(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[..., U],
        name: str | None = None,
    ) -> Node[U]: ...

    @overload
    def derived(
        self,
        deps: Iterable[Node[Any]],
        callback: None = None,
        name: str | None = None,
    ) -> Callable[[Callable[..., U]], Node[U]]: ...

    def derived(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[..., U] | None = None,
        name: str | None = None,
    ) -> Node[U] | Callable[[Callable[..., U]], Node[U]]:
        self._check_thread()
        deps = list(deps)
        if callback is None:
            return lambda fn: self.derived(deps, fn, name or fn.__name__)
        _reject_async_callable(callback)
        native_deps = self._native_deps(deps)

        def native_callback(*values: object) -> U:
            value = callback(*values)
            _reject_awaitable(value)
            _reject_sentinel_data(value)
            return value

        try:
            return Node(
                self._native.derived(native_deps, native_callback, name),
                owner_thread=self._owner_thread,
                lifetime=self._lifetime,
            )
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    @overload
    def node(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[[Ctx], object],
        name: str | None = None,
        *,
        partial: bool = False,
        complete_when_deps_complete: bool = True,
        error_when_deps_error: bool = True,
        terminal_as_real_input: bool = False,
        pausable: PausableMode = True,
        pull_id: str | None = None,
        restore: RestoreRef | None = None,
    ) -> Node[object]: ...

    @overload
    def node(
        self,
        deps: Iterable[Node[Any]],
        callback: None = None,
        name: str | None = None,
        *,
        partial: bool = False,
        complete_when_deps_complete: bool = True,
        error_when_deps_error: bool = True,
        terminal_as_real_input: bool = False,
        pausable: PausableMode = True,
        pull_id: str | None = None,
        restore: RestoreRef | None = None,
    ) -> Callable[[Callable[[Ctx], object]], Node[object]]: ...

    def node(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[[Ctx], object] | None = None,
        name: str | None = None,
        *,
        partial: bool = False,
        complete_when_deps_complete: bool = True,
        error_when_deps_error: bool = True,
        terminal_as_real_input: bool = False,
        pausable: PausableMode = True,
        pull_id: str | None = None,
        restore: RestoreRef | None = None,
    ) -> Node[object] | Callable[[Callable[[Ctx], object]], Node[object]]:
        self._check_thread()
        deps = list(deps)
        _reject_non_bool("partial", partial)
        _reject_non_bool("complete_when_deps_complete", complete_when_deps_complete)
        _reject_non_bool("error_when_deps_error", error_when_deps_error)
        _reject_non_bool("terminal_as_real_input", terminal_as_real_input)
        native_pausable = _native_pausable(pausable)
        if pull_id is not None:
            _reject_non_string_pull_id(pull_id)
        if pull_id is not None and pausable is False:
            msg = "pull_id nodes cannot use pausable=False in the Python facade"
            raise GraphReflyValueError(msg)
        if restore is not None and not isinstance(restore, RestoreRef):
            msg = "restore must be created by graphrefly.restore_ref(...)"
            raise GraphReflyValueError(msg)
        if callback is None:
            return lambda fn: self.node(
                deps,
                fn,
                name or fn.__name__,
                partial=partial,
                complete_when_deps_complete=complete_when_deps_complete,
                error_when_deps_error=error_when_deps_error,
                terminal_as_real_input=terminal_as_real_input,
                pausable=pausable,
                pull_id=pull_id,
                restore=restore,
            )
        _reject_async_callable(callback)
        native_deps = self._native_deps(deps)

        def native_callback(native_ctx: _native.Ctx) -> None:
            value = callback(
                Ctx(
                    native_ctx,
                    owner_thread=self._owner_thread,
                    lifetime=self._lifetime,
                )
            )
            _reject_awaitable(value)

        try:
            return Node(
                self._native.node(
                    native_deps,
                    native_callback,
                    name,
                    partial,
                    complete_when_deps_complete,
                    error_when_deps_error,
                    terminal_as_real_input,
                    native_pausable,
                    pull_id,
                    restore.ref if restore is not None else None,
                    restore.config if restore is not None else None,
                    restore.config_version if restore is not None else None,
                ),
                owner_thread=self._owner_thread,
                lifetime=self._lifetime,
            )
        except ValueError as error:
            raise GraphReflyValueError(str(error)) from error
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    @overload
    def effect(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[..., object],
        name: str | None = None,
    ) -> Node[object]: ...

    @overload
    def effect(
        self,
        deps: Iterable[Node[Any]],
        callback: None = None,
        name: str | None = None,
    ) -> Callable[[Callable[..., object]], Node[object]]: ...

    def effect(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[..., object] | None = None,
        name: str | None = None,
    ) -> Node[object] | Callable[[Callable[..., object]], Node[object]]:
        self._check_thread()
        deps = list(deps)
        if callback is None:
            return lambda fn: self.effect(deps, fn, name or fn.__name__)
        _reject_async_callable(callback)
        native_deps = self._native_deps(deps)

        def native_callback(*values: object) -> None:
            value = callback(*values)
            _reject_awaitable(value)

        try:
            return Node(
                self._native.effect(native_deps, native_callback, name),
                owner_thread=self._owner_thread,
                lifetime=self._lifetime,
            )
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def batch(self, callback: Callable[[], object]) -> None:
        self._check_thread()
        _reject_async_callable(callback)

        def native_callback() -> None:
            result = callback()
            _reject_awaitable(result)

        try:
            self._native.batch(native_callback)
        except ValueError:
            raise
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def describe(self) -> dict[str, Any]:
        self._check_thread()
        try:
            return _normalize_describe(self._native.describe())
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def pause(self, node: Node[Any], lock_id: str) -> None:
        self._check_thread()
        _reject_non_string_lock_id(lock_id)
        native = self._native_node(node)
        try:
            native.pause(lock_id)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def resume(self, node: Node[Any], lock_id: str) -> None:
        self._check_thread()
        _reject_non_string_lock_id(lock_id)
        native = self._native_node(node)
        try:
            native.resume(lock_id)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def invalidate(self, node: Node[Any]) -> None:
        self._check_thread()
        native = self._native_node(node)
        try:
            native.invalidate()
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def observe(
        self,
        callback: Callable[[GraphEvent], object],
        *,
        on_error: ObserverErrorHandler | None = None,
    ) -> Subscription:
        self._check_thread()
        _reject_async_callable(callback)
        if on_error is not None:
            _reject_async_callable(on_error)
        callback_errors: list[SubscriberCallbackError] = []

        def native_callback(path: str, kind: str, value: object, tier: int, seq: int) -> None:
            event = GraphEvent(
                path=path,
                message=_message_from_native(kind, value),
                tier=tier,
                seq=seq,
            )
            try:
                result = callback(event)
                _reject_awaitable(result)
            except Exception as error:
                _record_observer_error(error, callback_errors, on_error)

        try:
            subscription = Subscription(
                self._native.observe(native_callback),
                owner_thread=self._owner_thread,
                lifetime=self._lifetime,
                callback_errors=callback_errors,
            )
            self._lifetime.register(subscription)
            return subscription
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def _native_node(self, node: Node[Any]) -> _native.Node:
        node._check_thread()
        if node._lifetime is not self._lifetime:
            msg = "node must belong to this GraphReFly graph"
            raise GraphReflyRuntimeError(msg)
        return node._native

    def _native_deps(self, deps: Iterable[Node[Any]]) -> list[_native.Node]:
        native_deps = []
        for dep in deps:
            native_deps.append(self._native_node(dep))
        return native_deps

    def _conformance_find(self, id: str) -> Node[Any]:
        self._check_thread()
        if not isinstance(id, str):
            msg = "conformance id must be a str"
            raise GraphReflyValueError(msg)
        try:
            native = self._native._find(id)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)
        if native is None:
            msg = f"conformance node '{id}' was not found"
            raise GraphReflyRuntimeError(msg)
        return Node(native, owner_thread=self._owner_thread, lifetime=self._lifetime)

    def _conformance_set_state(self, id: str, value: object) -> None:
        self._check_thread()
        if not isinstance(id, str):
            msg = "conformance id must be a str"
            raise GraphReflyValueError(msg)
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        try:
            self._native._set_state_by_id(id, value)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def _check_thread(self, *, allow_closed: bool = False) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python graphs are bound to their creating thread in v0"
            raise GraphReflyRuntimeError(msg)
        if self._lifetime.is_closed() and not allow_closed:
            raise GraphReflyRuntimeError(self._lifetime.closed_message)


def from_awaitable[T](
    graph: Graph,
    runner: AsyncRunner,
    factory: Callable[[], Awaitable[T]],
    *,
    name: str | None = None,
    pausable: PausableMode = True,
) -> Node[T]:
    """Create a source from a fresh awaitable produced on each activation."""

    _validate_async_source_args(graph, runner, factory)
    native_pausable = _native_pausable(pausable)
    reentry_queue = _runner_reentry_queue(graph, runner)

    def activate(native_ctx: Any) -> None:
        if reentry_queue is not None and not reentry_queue._can_accept_activation():
            return
        native_gate = _AsyncCompletionGate(
            native_ctx,
            owner_thread=graph._owner_thread,
            lifetime=graph._lifetime,
            reentry_queue=reentry_queue,
        )
        job = _AsyncJob(runner, graph._lifetime, reentry_queue)
        if not job.active:
            native_gate.close()
            return
        native_gate.register_deactivation(job.cancel)
        completion_gate = (
            native_gate.queued_proxy() if reentry_queue is not None else native_gate
        )

        async def run() -> None:
            try:
                if not job.active:
                    return
                awaitable = factory()
                if not isawaitable(awaitable):
                    msg = "from_awaitable factory must return an awaitable"
                    raise GraphReflyRuntimeError(msg)
                value = await awaitable
                completion_gate.resolve(value, job.can_apply_completion)
            except Exception as error:
                if not _should_suppress_async_error(job, error):
                    completion_gate.error(error, job.can_apply_completion)
            except BaseException as error:
                if not _should_suppress_async_error(job, error):
                    completion_gate.fatal(error, job.can_apply_completion)
            finally:
                job.finish()

        job.start(run)

    try:
        return Node(
            graph._native._async_source(activate, name, native_pausable),
            owner_thread=graph._owner_thread,
            lifetime=graph._lifetime,
        )
    except RuntimeError as error:
        raise GraphReflyRuntimeError(str(error)) from error
    except BaseException as error:
        _poison_on_fatal(graph._lifetime, error)


def from_async_iter[T](
    graph: Graph,
    runner: AsyncRunner,
    factory: Callable[[], AsyncIterable[T]],
    *,
    name: str | None = None,
    pausable: PausableMode = True,
) -> Node[T]:
    """Create a source from a fresh async iterable produced on each activation."""

    _validate_async_source_args(graph, runner, factory)
    native_pausable = _native_pausable(pausable)
    reentry_queue = _runner_reentry_queue(graph, runner)

    def activate(native_ctx: Any) -> None:
        if reentry_queue is not None and not reentry_queue._can_accept_activation():
            return
        native_gate = _AsyncCompletionGate(
            native_ctx,
            owner_thread=graph._owner_thread,
            lifetime=graph._lifetime,
            reentry_queue=reentry_queue,
        )
        job = _AsyncJob(runner, graph._lifetime, reentry_queue)
        if not job.active:
            native_gate.close()
            return
        native_gate.register_deactivation(job.cancel)
        completion_gate = (
            native_gate.queued_proxy() if reentry_queue is not None else native_gate
        )

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
                    completion_gate.emit(value, job.can_apply_completion)
                completion_gate.complete(job.can_apply_completion)
            except Exception as error:
                if not _should_suppress_async_error(job, error):
                    completion_gate.error(error, job.can_apply_completion)
            except BaseException as error:
                if not _should_suppress_async_error(job, error):
                    completion_gate.fatal(error, job.can_apply_completion)
            finally:
                job.finish()

        job.start(run)

    try:
        return Node(
            graph._native._async_source(activate, name, native_pausable),
            owner_thread=graph._owner_thread,
            lifetime=graph._lifetime,
        )
    except RuntimeError as error:
        raise GraphReflyRuntimeError(str(error)) from error
    except BaseException as error:
        _poison_on_fatal(graph._lifetime, error)


def async_node[T](
    graph: Graph,
    deps: Iterable[Node[Any]],
    runner: AsyncRunner,
    callback: Callable[..., Awaitable[T]],
    *,
    name: str | None = None,
    pausable: PausableMode = True,
) -> Node[T]:
    """Create a value-level async compute node over declared dependencies."""

    graph._check_thread()
    deps = list(deps)
    _reject_async_factory_instance(callback)
    if not callable(callback):
        msg = "async_node callback must be callable"
        raise GraphReflyValueError(msg)
    _validate_runner(runner)
    native_deps = graph._native_deps(deps)
    native_pausable = _native_pausable(pausable)
    reentry_queue = _runner_reentry_queue(graph, runner)
    generation = 0

    def activate(native_ctx: Any, *values: object) -> None:
        nonlocal generation
        if reentry_queue is not None and not reentry_queue._can_accept_activation():
            return
        generation += 1
        invocation_generation = generation
        native_gate = _AsyncCompletionGate(
            native_ctx,
            owner_thread=graph._owner_thread,
            lifetime=graph._lifetime,
            reentry_queue=reentry_queue,
        )
        job = _AsyncJob(runner, graph._lifetime, reentry_queue)
        if not job.active:
            native_gate.close()
            return
        native_gate.register_deactivation(job.cancel)
        completion_gate = (
            native_gate.queued_proxy() if reentry_queue is not None else native_gate
        )

        def is_current() -> bool:
            return invocation_generation == generation and job.can_apply_completion()

        async def run() -> None:
            try:
                if not job.active:
                    return
                awaitable = callback(*values)
                if not isawaitable(awaitable):
                    msg = "async_node callback must return an awaitable"
                    raise GraphReflyRuntimeError(msg)
                value = await awaitable
                completion_gate.emit(value, is_current, final=True)
            except Exception as error:
                if not _should_suppress_async_error(job, error):
                    completion_gate.error(error, is_current)
            except BaseException as error:
                if not _should_suppress_async_error(job, error):
                    completion_gate.fatal(error, is_current)
            finally:
                job.finish()

        job.start(run)

    try:
        return Node(
            graph._native._async_node(native_deps, activate, name, native_pausable),
            owner_thread=graph._owner_thread,
            lifetime=graph._lifetime,
        )
    except RuntimeError as error:
        raise GraphReflyRuntimeError(str(error)) from error
    except BaseException as error:
        _poison_on_fatal(graph._lifetime, error)


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


def version() -> str:
    """Return the installed Python package version."""

    try:
        return metadata.version("graphrefly")
    except PackageNotFoundError:
        return _VERSION


def wire_bridge(graph: Graph, *, session_id: str, name: str | None = None) -> WireBridge:
    if not isinstance(graph, Graph):
        raise GraphReflyValueError("wire_bridge requires a graphrefly.Graph")
    graph._check_thread()
    if not isinstance(session_id, str) or session_id == "":
        raise GraphReflyValueError("wire_bridge session_id must be a non-empty str")
    native = graph._native._wire_bridge(session_id, name)
    return WireBridge(native, graph)


def wire_bridge_protobuf(
    graph: Graph,
    bridge: WireBridge,
    *,
    name: str | None = None,
) -> WireBridgeProtobuf:
    if not isinstance(graph, Graph):
        raise GraphReflyValueError("wire_bridge_protobuf requires a graphrefly.Graph")
    graph._check_thread()
    bridge._check_attachable()
    if bridge._lifetime is not graph._lifetime:
        raise GraphReflyRuntimeError("wire_bridge_protobuf bridge must belong to graph")
    native = graph._native._wire_bridge_protobuf(bridge._native, name)
    return WireBridgeProtobuf(native, graph)


def wire_edge_group(
    graph: Graph,
    bridge: WireBridge,
    *,
    name: str | None = None,
    inbound_edges: Iterable[str] | None = None,
    outbound_edges: dict[str, Node[bytes]] | None = None,
) -> WireEdgeGroup:
    if not isinstance(graph, Graph):
        raise GraphReflyValueError("wire_edge_group requires a graphrefly.Graph")
    graph._check_thread()
    bridge._check_attachable()
    if bridge._lifetime is not graph._lifetime:
        raise GraphReflyRuntimeError("wire_edge_group bridge must belong to graph")
    has_inbound = inbound_edges is not None
    has_outbound = outbound_edges is not None
    if has_inbound == has_outbound:
        raise GraphReflyValueError(
            "wire_edge_group requires exactly one of inbound_edges or outbound_edges"
        )
    if outbound_edges is not None:
        raise GraphReflyRuntimeError(
            "wire_edge_group outbound_edges facade is pending native bytes-proxy coverage"
        )
    edges = list(inbound_edges or ())
    if not edges or any(not isinstance(edge, str) or edge == "" for edge in edges):
        raise GraphReflyValueError("wire_edge_group inbound_edges must be non-empty strings")
    native = graph._native._wire_edge_group(bridge._native, edges, name)
    return WireEdgeGroup(native, graph)


def wire_bridge_ack_driver(
    graph: Graph,
    bridge: WireBridge,
    *,
    clock: Node[int],
    timeout_ms: int,
    name: str | None = None,
) -> WireBridgeAckDriver:
    if not isinstance(graph, Graph):
        raise GraphReflyValueError("wire_bridge_ack_driver requires a graphrefly.Graph")
    graph._check_thread()
    bridge._check_attachable()
    graph._native_node(clock)
    if bridge._lifetime is not graph._lifetime:
        raise GraphReflyRuntimeError("wire_bridge_ack_driver bridge must belong to graph")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 0:
        raise GraphReflyValueError("wire_bridge_ack_driver timeout_ms must be non-negative int")
    native = graph._native._wire_bridge_ack_driver(clock._native, timeout_ms, name)
    return WireBridgeAckDriver(native, graph)


def restore_ref(
    ref: str,
    *,
    config: object | None = None,
    config_version: object | None = None,
) -> RestoreRef:
    """Return strict-JSON factory metadata for a restorable function-backed node."""

    if not isinstance(ref, str) or ref == "":
        msg = "restore ref must be a non-empty str"
        raise GraphReflyValueError(msg)
    _reject_awaitable(config)
    _reject_awaitable(config_version)
    _reject_sentinel_data(config)
    _reject_sentinel_data(config_version)
    return RestoreRef(ref=ref, config=config, config_version=config_version)


def restore_registry(
    entries: Iterable[RestoreDescriptor],
    *,
    include_builtins: bool = True,
) -> RestoreRegistry:
    """Build an explicit restore registry for `restore_graph`."""

    _reject_non_bool("include_builtins", include_builtins)
    entry_list = list(entries)
    _validate_restore_registry_entries(entry_list, include_builtins=include_builtins)
    return _RestoreRegistryFacade(entry_list, include_builtins=include_builtins)


def restore_graph(checkpoint: GraphCheckpoint, *, registry: RestoreRegistry) -> Graph:
    """Construct a fresh graph from an already-loaded strict-JSON checkpoint."""

    if not isinstance(registry, _RestoreRegistryFacade):
        msg = "registry must be created by graphrefly.restore_registry(...)"
        raise GraphReflyRestoreError(msg)
    owner_thread = get_ident()
    lifetime = _GraphLifetime(owner_thread)
    try:
        native_registry = registry._native_registry(
            owner_thread=owner_thread,
            lifetime=lifetime,
        )
        native_graph = _native.restore_graph(checkpoint, native_registry)
        return Graph._from_native(
            native_graph,
            owner_thread=owner_thread,
            lifetime=lifetime,
        )
    except ValueError as error:
        raise GraphReflyRestoreError(str(error)) from error
    except TypeError as error:
        raise GraphReflyRestoreError(str(error)) from error
    except RuntimeError as error:
        raise GraphReflyRestoreError(str(error)) from error


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


def _is_cancellation_error(error: BaseException) -> bool:
    if isinstance(error, BaseExceptionGroup):
        return bool(error.exceptions) and all(
            _is_cancellation_error(item) for item in error.exceptions
        )
    return "Cancel" in type(error).__name__


def _format_async_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _poison_on_fatal(lifetime: _GraphLifetime, error: BaseException) -> NoReturn:
    if not isinstance(error, Exception):
        lifetime.poison()
    raise error


def _reject_non_string_lock_id(lock_id: str) -> None:
    if not isinstance(lock_id, str):
        msg = "pause/resume lock_id must be a str in the Python facade"
        raise GraphReflyValueError(msg)


def _reject_non_string_pull_id(pull_id: str) -> None:
    if not isinstance(pull_id, str):
        msg = "pull_id must be a str in the Python facade"
        raise GraphReflyValueError(msg)


def _native_pausable(pausable: PausableMode) -> Literal["true", "resumeAll", "false"]:
    if pausable is True:
        return "true"
    if pausable is False:
        return "false"
    if pausable == "resumeAll":
        return "resumeAll"
    msg = "pausable must be True, False, or 'resumeAll' in the Python facade"
    raise GraphReflyValueError(msg)


def _normalize_toward_dep(toward_dep: int | None) -> int | None:
    if toward_dep is None:
        return None
    if isinstance(toward_dep, bool) or not isinstance(toward_dep, int):
        msg = "toward_dep must be an int dependency index in the Python facade"
        raise GraphReflyValueError(msg)
    if toward_dep < 0:
        msg = "toward_dep must be non-negative in the Python facade"
        raise GraphReflyValueError(msg)
    return toward_dep


def _reject_non_bool(name: str, value: bool) -> None:
    if not isinstance(value, bool):
        msg = f"{name} must be a bool in the Python facade"
        raise GraphReflyValueError(msg)


def _reject_sentinel_data(value: object) -> None:
    if value is SENTINEL:
        msg = "graphrefly.SENTINEL is a protocol absence marker and cannot be DATA"
        raise GraphReflyValueError(msg)


def _reject_awaitable(value: object) -> None:
    if isawaitable(value):
        close = getattr(value, "close", None)
        if callable(close):
            close()
        msg = "async callbacks are deferred to a later adapter slice"
        raise CallbackError(msg)


def _reject_async_callable(callback: Callable[..., object]) -> None:
    if iscoroutinefunction(callback):
        msg = "async callbacks are deferred to a later adapter slice"
        raise GraphReflyRuntimeError(msg)


def _validate_rewire_callback(callback: object) -> NodeCallback:
    if not callable(callback):
        msg = "rewire_next callback must be callable in the Python facade"
        raise GraphReflyValueError(msg)
    typed = cast("NodeCallback", callback)
    _reject_async_callable(typed)
    return typed


def _message_from_native(kind: str, value: object) -> Message[Any]:
    if kind == "DATA":
        return DataMessage(value)
    if kind == "ERROR":
        return ErrorMessage(_graph_callback_error(value))
    return ControlMessage(kind=kind, value=value)


def _graph_callback_error(value: object) -> GraphCallbackError:
    if isinstance(value, GraphCallbackError):
        return value
    text = str(value)
    type_name, separator, message = text.partition(": ")
    if not separator:
        type_name = "Exception"
        message = text
    return GraphCallbackError(type_name=type_name, message=message, raw=value)


def _record_observer_error(
    error: Exception,
    callback_errors: list[SubscriberCallbackError],
    on_error: ObserverErrorHandler | None,
) -> None:
    wrapped = (
        error
        if isinstance(error, SubscriberCallbackError)
        else SubscriberCallbackError(_scrub_exception(error))
    )
    _remember_callback_error(callback_errors, wrapped)
    if on_error is not None:
        try:
            result = on_error(wrapped)
            _reject_awaitable(result)
        except Exception as handler_error:
            _remember_callback_error(
                callback_errors,
                SubscriberCallbackError(_scrub_exception(handler_error)),
            )


def _remember_callback_error(
    callback_errors: list[SubscriberCallbackError],
    error: SubscriberCallbackError,
) -> None:
    callback_errors.append(error)
    if len(callback_errors) > _MAX_CALLBACK_ERRORS:
        del callback_errors[: len(callback_errors) - _MAX_CALLBACK_ERRORS]


def _scrub_exception(error: Exception) -> Exception:
    error.__traceback__ = None
    error.__context__ = None
    error.__cause__ = None
    return error


def _normalize_describe(snapshot: dict[str, Any]) -> dict[str, Any]:
    nodes = []
    for node in snapshot.get("nodes", []):
        normalized = dict(node)
        has_value = bool(normalized.get("has_value", normalized.get("value") is not None))
        normalized["has_value"] = has_value
        if not has_value:
            normalized.pop("value", None)
        nodes.append(normalized)

    normalized_snapshot = dict(snapshot)
    normalized_snapshot["nodes"] = nodes
    subgraphs = normalized_snapshot.get("subgraphs")
    if isinstance(subgraphs, list):
        normalized_snapshot["subgraphs"] = [
            _normalize_describe(cast("dict[str, Any]", subgraph)) for subgraph in subgraphs
        ]
    return normalized_snapshot
