"""Typed Python facade over the Python GraphReFly kernel (graphrefly-py:D2, D4)."""

from __future__ import annotations

import codecs
import logging
from collections import deque
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module, metadata
from importlib.metadata import PackageNotFoundError
from inspect import isawaitable, iscoroutine, iscoroutinefunction
from threading import Lock, get_ident
from typing import Any, Final, Literal, NoReturn, Protocol, TypeVar, cast, overload
from weakref import ReferenceType, ref

from graphrefly import _checkpoint
from graphrefly import _kernel as _k
from graphrefly._sentinel import SENTINEL
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
R = TypeVar("R")
_VERSION = "0.23.0"  # x-release-please-version
_SSE_PARSER_MAX_BUFFER_BYTES = 64 * 1024
_SSE_RETRY_MAX = (1 << 64) - 1
_NO_DEFAULT = object()
_ABSENT: Any = object()
_GRAPH_REENTRY_QUEUE_TOKEN = object()
_LOGGER = logging.getLogger("graphrefly")
#: The attribute on a kernel function or setup that holds the user's callback or setup.
_AUTHORED = "_graphrefly_authored"


#: The seven message types (R-messages); a message is a tuple whose first element is its
#: type: ``(DATA, value)``, ``(ERROR, error)``, or ``(TYPE,)`` for the others.
START: Final = "START"
PENDING: Final = "PENDING"
DATA: Final = "DATA"
RESOLVED: Final = "RESOLVED"
INVALIDATE: Final = "INVALIDATE"
COMPLETE: Final = "COMPLETE"
ERROR: Final = "ERROR"
assert (START, PENDING, DATA, RESOLVED, INVALIDATE, COMPLETE, ERROR) == (
    _k.START,
    _k.PENDING,
    _k.DATA,
    _k.RESOLVED,
    _k.INVALIDATE,
    _k.COMPLETE,
    _k.ERROR,
)

type DataMessage[T] = tuple[Literal["DATA"], T]
type ErrorMessage = tuple[Literal["ERROR"], object]
type ControlMessage = tuple[Literal["START", "PENDING", "RESOLVED", "INVALIDATE", "COMPLETE"]]
type Message[T] = DataMessage[T] | ErrorMessage | ControlMessage
type Array[T] = tuple[Message[T], ...]
type NodeCallback = Callable[["Ctx"], object]
type SetupCallback = Callable[["Ctx"], object]
type AsyncJobFactory = Callable[[], Awaitable[None]]
type _CompletionPredicate = Callable[[], bool]
type _CancelScopeFactory = Callable[[], object]
type GraphCheckpoint = dict[str, Any]
type RecipeFactory = Callable[
    [Any, "RecipeContext"], NodeCallback | tuple[NodeCallback, SetupCallback | None]
]
type Policy = Literal["auto", "manual"]


@dataclass(frozen=True, slots=True)
class HostReport:
    """A failure with no caller to return to: a cleanup, a queued request, an observer."""

    source: str
    error: Exception


@dataclass(frozen=True, slots=True)
class NodeStatus:
    """A node's status, derived from what it holds and never stored."""

    cache: Literal["empty", "stale", "ready"]
    output: Literal["open", "completed", "errored"]
    lifecycle: Literal["inactive", "active", "closing"]


@dataclass(frozen=True, slots=True)
class Recipe:
    """How a restorable node is built: a registered factory reference and a JSON config."""

    ref: str
    config: object = None


@dataclass(frozen=True, slots=True)
class RecipeContext:
    """What a recipe factory gets besides its config: the graph being restored, the
    node's name, and ``make(ref, config, name)``, which builds another registered recipe
    and returns its ``(callback, setup)`` (R-checkpoint)."""

    graph: Graph
    name: str
    make: Callable[[str, object, str], tuple[NodeCallback, SetupCallback | None]]


@dataclass(frozen=True, slots=True)
class NodeDefinition:
    """A node's current definition: declared dependencies, callback, setup, recipe and
    policies. Reading it changes nothing."""

    deps: tuple[Node[Any], ...]
    callback: Callable[..., object] | None
    setup: SetupCallback | None
    recipe: Recipe | None
    complete: Policy
    error: Policy
    invalidate: Policy
    quiet: bool


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """Host-driver HTTP request material for Python network source adapters."""

    method: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Host-driver HTTP response material emitted by `from_http`."""

    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class HttpStreamHead:
    """HTTP response head material for streaming source drivers."""

    status: int
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class HttpStreamHeadEvent:
    """Streaming driver event carrying the HTTP response head."""

    head: HttpStreamHead
    kind: Literal["Head"] = "Head"


@dataclass(frozen=True, slots=True)
class HttpStreamChunkEvent:
    """Streaming driver event carrying a bytes chunk."""

    chunk: bytes
    kind: Literal["Chunk"] = "Chunk"


@dataclass(frozen=True, slots=True)
class HttpStreamErrorEvent:
    """Streaming driver event carrying a host-side stream error."""

    error: BaseException
    kind: Literal["Error"] = "Error"


@dataclass(frozen=True, slots=True)
class HttpStreamCompleteEvent:
    """Streaming driver event marking normal stream completion."""

    kind: Literal["Complete"] = "Complete"


@dataclass(frozen=True, slots=True)
class SseEvent:
    """Parsed server-sent event DATA emitted by `from_sse`."""

    data: str
    event: str | None = None
    id: str | None = None
    retry_ms: int | None = None


type HttpRequestLike = str | HttpRequest
type HttpStreamDriverEvent = (
    HttpStreamHeadEvent | HttpStreamChunkEvent | HttpStreamErrorEvent | HttpStreamCompleteEvent
)


class LocalHttpDriver(Protocol):
    """Host-owned one-shot HTTP driver used by `from_http`."""

    def request(self, request: HttpRequest) -> Awaitable[HttpResponse]:
        """Start one HTTP request and resolve to an `HttpResponse`."""
        ...


class LocalHttpStreamDriver(Protocol):
    """Host-owned streaming HTTP driver used by `from_sse`."""

    def stream(self, request: HttpRequest) -> AsyncIterable[HttpStreamDriverEvent]:
        """Start one HTTP stream and yield normalized stream events."""
        ...


class AsyncRunner(Protocol):
    """Framework-neutral async job runner supplied explicitly by the host."""

    def spawn(self, job: AsyncJobFactory) -> object:
        """Schedule one async job in the caller-owned runtime."""
        ...


class _ResourceScope:
    def __init__(self, closed_message: Callable[[], str]) -> None:
        self._closed_message = closed_message
        self._lock = Lock()
        self._closed = False
        self._resources: list[object] = []

    def register(self, resource: object) -> None:
        should_close = False
        with self._lock:
            if self._closed:
                should_close = True
            elif resource not in self._resources:
                self._resources.append(resource)
        if should_close:
            _close_lifecycle_resource(resource)
            raise GraphReflyRuntimeError(self._closed_message())

    def adopt(self, resource: object, *, lifetime: _GraphLifetime) -> None:
        lifetime.unregister_resource(resource)
        self.register(resource)

    def unregister(self, resource: object) -> None:
        with self._lock:
            self._resources = [item for item in self._resources if item is not resource]

    def retained_subscriptions(self) -> tuple[Subscription, ...]:
        with self._lock:
            resources = tuple(self._resources)
        subscriptions: list[Subscription] = []
        for resource in resources:
            if isinstance(resource, Retain):
                subscriptions.append(resource._subscription)
            scope = getattr(resource, "_scope", None)
            if isinstance(scope, _ResourceScope):
                subscriptions.extend(scope.retained_subscriptions())
        return tuple(subscriptions)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            resources = tuple(reversed(self._resources))
            self._resources.clear()
        first_error: BaseException | None = None
        for resource in resources:
            try:
                _close_lifecycle_resource(resource)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


def _close_lifecycle_resource(resource: object) -> None:
    close = getattr(resource, "_close_from_graph", None)
    if callable(close):
        close()


def _record_lifecycle_error(
    first_error: BaseException | None,
    cleanup: Callable[[], object],
) -> BaseException | None:
    try:
        cleanup()
    except BaseException as error:
        if first_error is None:
            return error
    return first_error


class _GraphLifetime:
    def __init__(self, owner_thread: int) -> None:
        self.owner_thread = owner_thread
        self._lock = Lock()
        self.closed = False
        self.poisoned = False
        self.subscriptions: list[Subscription] = []
        self.async_jobs: list[ReferenceType[_AsyncJob]] = []
        self.reentry_queues: list[ReferenceType[GraphReentryQueue]] = []
        self.resources = _ResourceScope(lambda: self.closed_message)

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
                self.subscriptions.append(subscription)
        if should_close:
            subscription._close_from_graph()
            raise GraphReflyRuntimeError(self.closed_message)

    def unregister(self, subscription: Subscription) -> None:
        with self._lock:
            self.subscriptions = [item for item in self.subscriptions if item is not subscription]

    def register_retain(self, retain: Retain) -> None:
        self.register_resource(retain)

    def unregister_retain(self, retain: Retain) -> None:
        self.unregister_resource(retain)

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
                item for item in self.async_jobs if (live := item()) is not None and live is not job
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
                _close_lifecycle_resource(resource)
                raise GraphReflyRuntimeError(self.closed_message)
        self.resources.register(resource)

    def unregister_resource(self, resource: object) -> None:
        self.resources.unregister(resource)

    def new_resource_scope(self) -> _ResourceScope:
        return _ResourceScope(lambda: self.closed_message)

    def close(self, *, suppress_errors: bool = False) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            queues = tuple(item() for item in self.reentry_queues)
            jobs = tuple(item() for item in self.async_jobs)
            retained_subscriptions = self.resources.retained_subscriptions()
            subscriptions = tuple(
                subscription
                for subscription in self.subscriptions
                if all(subscription is not retained for retained in retained_subscriptions)
            )
            self.reentry_queues.clear()
            self.async_jobs.clear()
            self.subscriptions.clear()
        first_error: BaseException | None = None
        for subscription in subscriptions:
            try:
                subscription._close_from_graph()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        self._close_reentry_queues(queues)
        self._cancel_async_jobs(jobs)
        try:
            self.resources.close()
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
    """One array a node published, as Graph.observe sees it."""

    path: str
    messages: Array[Any]
    seq: int


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


class Subscription:
    """Idempotent subscription handle returned by observe and subscribe calls."""

    def __init__(
        self,
        detach: Callable[[], None],
        *,
        owner_thread: int,
        lifetime: _GraphLifetime | None = None,
    ) -> None:
        self._detach = detach
        self._owner_thread = owner_thread
        self._lifetime = lifetime
        self._closed = False

    @property
    def closed(self) -> bool:
        """Return whether this subscription has been unsubscribed."""

        return self._closed

    def unsubscribe(self) -> None:
        """Detach this subscription from its graph node or observer."""

        self._check_thread()
        if not self._closed:
            self._detach_now()
            if self._lifetime is not None:
                self._lifetime.unregister(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.unsubscribe()

    def _check_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python subscriptions are bound to their creating thread"
            raise GraphReflyRuntimeError(msg)

    def _close_from_graph(self) -> None:
        if not self._closed:
            self._detach_now()

    def _detach_now(self) -> None:
        try:
            self._detach()
        except _k.KernelError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except _k.FATAL as error:
            self._closed = True
            if self._lifetime is not None:
                _poison_on_fatal(self._lifetime, error)
            raise
        except Exception:
            self._closed = True
            raise
        except BaseException as error:
            self._closed = True
            if self._lifetime is not None:
                _poison_on_fatal(self._lifetime, error)
            raise
        self._closed = True


class Retain:
    """Graph-owned activation root release token."""

    def __init__(
        self,
        subscription: Subscription,
        *,
        owner_thread: int,
        lifetime: _GraphLifetime,
    ) -> None:
        self._subscription = subscription
        self._owner_thread = owner_thread
        self._lifetime = lifetime
        self._closed = False

    @property
    def closed(self) -> bool:
        """Return whether this retain token has been released."""

        return self._closed

    def release(self) -> None:
        """Release this graph-owned activation root."""

        self._check_thread()
        if not self._closed:
            self._subscription.unsubscribe()
            self._closed = True
            self._lifetime.unregister_retain(self)

    def __enter__(self) -> Retain:
        self._check_thread()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    def _close_from_graph(self) -> None:
        if not self._closed:
            self._subscription._close_from_graph()
            self._closed = True

    def _check_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python retain handles are bound to their creating thread in v0"
            raise GraphReflyRuntimeError(msg)


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


class Dep:
    """One dependency as an invocation sees it, ``ctx.deps[i]`` (R-invocation).

    ``id`` is the dependency's name. ``waves`` are the arrays it delivered to this
    invocation, each as its DATA values in order (a RESOLVED array is ``[]``);
    ``terminal`` is its COMPLETE or ERROR of this invocation, or ``None``; ``has`` says
    whether a value was received and not invalidated, and ``value`` is the latest one,
    or ``graphrefly.SENTINEL`` without one (``None`` is valid DATA); ``invalidated`` says
    whether an INVALIDATE arrived on it, under the manual INVALIDATE policy.
    """

    __slots__ = ("_dep",)

    def __init__(self, dep: _k.Dep) -> None:
        self._dep = dep

    @property
    def id(self) -> str:
        """Return the dependency's name."""

        return self._dep.id

    @property
    def waves(self) -> list[list[object]]:
        """Return this invocation's arrays from the dependency, as their DATA values."""

        return self._dep.waves

    @property
    def terminal(self) -> ControlMessage | ErrorMessage | None:
        """Return the dependency's COMPLETE or ERROR of this invocation, or ``None``."""

        return cast("ControlMessage | ErrorMessage | None", self._dep.terminal)

    @property
    def has(self) -> bool:
        """Return whether a value was received from the dependency and not invalidated."""

        return self._dep.has

    @property
    def value(self) -> object:
        """Return the latest value received, or ``graphrefly.SENTINEL`` without one."""

        dep = self._dep
        return dep.value if dep.has else SENTINEL

    @property
    def invalidated(self) -> bool:
        """Return whether an INVALIDATE arrived on this input (manual policy only)."""

        return self._dep.invalidated

    def __repr__(self) -> str:
        return f"Dep({self.id!r})"


class Ctx:
    """What an advanced node callback receives for one invocation.

    ``deps`` reads the invocation's inputs, ``state`` keeps the lifetime's private
    state, and :meth:`emit` and :meth:`down` send output. It never sends anything
    upstream.
    """

    __slots__ = ("_ctx", "_deps", "_graph")

    def __init__(self, ctx: _k.Ctx, graph: Graph) -> None:
        self._ctx = ctx
        self._graph = graph
        self._deps: tuple[Dep, ...] | None = None

    @property
    def deps(self) -> tuple[Dep, ...]:
        """Return each dependency of this lifetime, in order, as this invocation sees it."""

        self._check()
        deps = self._deps
        if deps is None:
            deps = self._deps = tuple(Dep(dep) for dep in self._ctx.deps)
        return deps

    def emit(self, *values: object) -> None:
        """Send one array of DATA, one message per value."""

        self._check()
        if not values:
            msg = "emit needs at least one value"
            raise GraphReflyValueError(msg)
        for value in values:
            _reject_awaitable(value)
            _reject_sentinel_data(value)
        self._send([(_k.DATA, value) for value in values])

    def down(self, array: Sequence[Message[Any]]) -> None:
        """Send one array, a list of messages: ``(DATA, v)``, ``(ERROR, e)``,
        ``(INVALIDATE,)`` or ``(COMPLETE,)``.

        START, PENDING and RESOLVED are the kernel's alone and are rejected, as is a
        terminal that is not last; the kernel checks the array before anything is sent.
        """

        self._check()
        if not isinstance(array, list):
            msg = f"ctx.down takes one array, a list of message tuples, not {array!r}"
            raise GraphReflyValueError(msg)
        for message in array:
            if isinstance(message, tuple) and len(message) == 2 and message[0] == _k.DATA:
                _reject_awaitable(message[1])
        self._send(list(array))

    @property
    def has_state(self) -> bool:
        """Return whether this lifetime's private state is set (to anything but None)."""

        self._check()
        return self._ctx.state is not None

    @property
    def state(self) -> object | None:
        """Return this lifetime's private state."""

        self._check()
        return self._ctx.state

    @state.setter
    def state(self, value: object) -> None:
        self._check()
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        self._ctx.state = value

    @property
    def signal(self) -> _k.Signal:
        """Return the lifetime's abort signal, aborted when this lifetime closes."""

        self._check()
        return self._ctx.signal

    def rewire(self, deps: Iterable[Node[Any]]) -> None:
        """Change this node's own dependencies for its current lifetime.

        It runs after the current wave and is undone when the lifetime closes.
        """

        self._check()
        ids = [self._graph._node_id(dep) for dep in deps]
        try:
            self._ctx.rewire(ids)
        except _k.KernelError as error:
            raise GraphReflyValueError(str(error)) from error

    def _send(self, array: list[_k.Message]) -> None:
        try:
            self._ctx.down(array)
        except _k.KernelError as error:
            raise GraphReflyValueError(str(error)) from error

    def _check(self) -> None:
        # A cleanup that Graph.close runs may still use its ctx (R-lifetime); a ctx whose
        # lifetime has closed reads no state and sends nothing, so closed is no concern.
        self._graph._check_thread(allow_closed=True)


class Node[T]:
    """A typed handle to one node of a graph."""

    def __init__(self, graph: Graph, node_id: str) -> None:
        self._graph = graph
        self._id = node_id

    @property
    def name(self) -> str:
        """Return the node's name, its id in the graph."""

        return self._id

    @overload
    def cache(self) -> T: ...

    @overload
    def cache(self, default: U) -> T | U: ...

    def cache(self, default: object = _NO_DEFAULT) -> object:
        """Return the latest retained DATA value, or ``default`` when it holds none."""

        self._graph._check_thread()
        held = self._graph._kernel.get(self._id)
        if not held.has:
            if default is _NO_DEFAULT:
                msg = "node has no cached DATA value"
                raise GraphReflyNoDataError(msg)
            return default
        return cast("T", held.value)

    @property
    def has_value(self) -> bool:
        """Return whether this node retains a DATA value."""

        self._graph._check_thread()
        return self._graph._kernel.get(self._id).has

    @property
    def status(self) -> NodeStatus:
        """Return the node's derived status: cache, output and lifecycle."""

        self._graph._check_thread()
        status = self._graph._kernel.status(self._id)
        return NodeStatus(status.cache, status.output, status.lifecycle)

    def subscribe(self, callback: Callable[[Array[T]], object]) -> Subscription:
        """Subscribe to this node: ``callback`` gets each array as a tuple of messages.

        The first array is START followed by the node's snapshot. A callback that
        raises goes to the graph's reporter as a ``SubscriberCallbackError``; it is not
        the node's ERROR, and nothing keeps it.
        """

        graph = self._graph
        graph._check_thread()
        _reject_async_callable(callback)

        def sink(array: tuple[_k.Message, ...]) -> None:
            _observed(lambda: callback(cast("Array[T]", array)))

        sink_id = graph._new_sink_id()
        node_id = self._id
        subscription = Subscription(
            lambda: graph._kernel.unsubscribe(sink_id, node_id),
            owner_thread=graph._owner_thread,
            lifetime=graph._lifetime,
        )
        graph._lifetime.register(subscription)
        graph._call(lambda: graph._kernel.subscribe(sink_id, node_id, sink))
        return subscription

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._id!r})"


class State(Node[T]):
    """A state node: the only place host input enters a graph."""

    def set(self, value: T | Callable[[T], T]) -> None:
        """Write a value; given an updater, compute it from the state's value when the
        write runs in request order.

        An updater is a function, a method or a ``functools.partial``; a class or an
        object with ``__call__`` is written as a value. The updater gets
        ``graphrefly.SENTINEL`` when the state holds no value (not ``None``, which is
        valid DATA in Python; TS passes ``undefined`` and Rust ``None`` for the same
        absence). An updater that returns the value it was given writes nothing: the
        same object, or an equal ``bool``, ``int``, ``float``, ``str`` or ``bytes``
        (NaN counts as itself, ``-0.0`` differs from ``0.0``). To store a function as
        the value, wrap it: ``set(lambda _: fn)`` (graphrefly-py:D11).
        """

        graph = self._graph
        graph._check_thread()
        if _k.is_updater(value):
            fn = cast("Callable[[object], object]", value)
            _reject_async_callable(fn)

            def compute(previous: object) -> object:
                result = fn(previous)
                _reject_awaitable(result)
                return result

            graph._call(lambda: graph._kernel.set(self._id, compute))
            return
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        graph._call(lambda: graph._kernel.set(self._id, value))

    def invalidate(self) -> None:
        """Remove the state's value and invalidate its dependents."""

        graph = self._graph
        graph._check_thread()
        graph._call(lambda: graph._kernel.invalidate(self._id))

    def complete(self) -> None:
        """End the state with COMPLETE; later host input is refused."""

        graph = self._graph
        graph._check_thread()
        graph._call(lambda: graph._kernel.complete(self._id))

    def error(self, error: object) -> None:
        """End the state with ERROR carrying ``error``; later host input is refused."""

        graph = self._graph
        graph._check_thread()
        if isinstance(error, bool) or error is SENTINEL:
            msg = "an ERROR diagnostic cannot be a bool or SENTINEL"
            raise GraphReflyValueError(msg)
        graph._call(lambda: graph._kernel.error(self._id, error))


class Graph:
    """A graph of nodes: one single-threaded domain on the Python kernel."""

    def __init__(
        self,
        name: str | None = None,
        *,
        report: Callable[[HostReport], object] | None = None,
    ) -> None:
        self._owner_thread = get_ident()
        self._lifetime = _GraphLifetime(self._owner_thread)
        self._name = name
        self._report_to = report
        self._kernel = _k.Graph(report=self._on_report)
        self._nodes: dict[str, Node[Any]] = {}
        self._next_id = 0
        self._next_sink = 0
        self._reentry_queue: GraphReentryQueue | None = None

    def __enter__(self) -> Graph:
        self._check_thread()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @property
    def name(self) -> str | None:
        """Return the graph's name."""

        return self._name

    @property
    def closed(self) -> bool:
        """Return whether this graph facade has been closed."""

        self._check_thread(allow_closed=True)
        return self._lifetime.is_closed()

    def close(self) -> None:
        """Close this graph: release facade subscriptions, retains and async work."""

        self._check_thread(allow_closed=True)
        self._lifetime.close()

    # ---- construction ----------------------------------------------------------

    @overload
    def state(self, value: T, name: str | None = None) -> State[T]: ...

    @overload
    def state(self, *, name: str | None = None) -> State[Any]: ...

    def state(self, value: object = _ABSENT, name: str | None = None) -> State[Any]:
        """Create a state node, with an initial value or empty."""

        self._check_thread()
        node_id = self._new_id(name)
        if value is _ABSENT:
            self._call(lambda: self._kernel.state(node_id))
        else:
            _reject_awaitable(value)
            _reject_sentinel_data(value)
            self._call(lambda: self._kernel.state(node_id, value))
        return cast("State[Any]", self._register(State(self, node_id)))

    @overload
    def producer(self, callback: Callable[[], T], name: str | None = None) -> Node[T]: ...

    @overload
    def producer(
        self, callback: None = None, name: str | None = None
    ) -> Callable[[Callable[[], T]], Node[T]]: ...

    def producer(
        self, callback: Callable[[], T] | None = None, name: str | None = None
    ) -> Node[T] | Callable[[Callable[[], T]], Node[T]]:
        """Create or decorate a dependency-free node whose callback's value it sends
        once per activation (Python sugar)."""

        self._check_thread()
        if callback is None:
            return lambda fn: self.producer(fn, name or fn.__name__)
        _reject_async_callable(callback)
        produce = callback

        def run(ctx: _k.Ctx) -> None:
            value = _guarded(produce)
            _reject_awaitable(value)
            _reject_sentinel_data(value)
            ctx.down([(_k.DATA, value)])

        return self._add_node(name, [], run, callback, None)

    @overload
    def derived(
        self, deps: Iterable[Node[Any]], callback: Callable[..., U], name: str | None = None
    ) -> Node[U]: ...

    @overload
    def derived(
        self, deps: Iterable[Node[Any]], callback: None = None, name: str | None = None
    ) -> Callable[[Callable[..., U]], Node[U]]: ...

    def derived(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[..., U] | None = None,
        name: str | None = None,
    ) -> Node[U] | Callable[[Callable[..., U]], Node[U]]:
        """Create or decorate a value-level node over dependencies.

        Python sugar, not protocol: it runs once every dependency has a value and
        recomputes when a dependency sent DATA in the wave.
        """

        self._check_thread()
        dep_list = list(deps)
        if callback is None:
            return lambda fn: self.derived(dep_list, fn, name or fn.__name__)
        _reject_async_callable(callback)
        compute = callback

        def run(ctx: _k.Ctx) -> None:
            values = _gate(ctx)
            if values is None:
                return
            value = _guarded(lambda: compute(*values))
            _reject_awaitable(value)
            _reject_sentinel_data(value)
            ctx.down([(_k.DATA, value)])

        return self._add_node(name, dep_list, run, callback, None)

    @overload
    def effect(
        self, deps: Iterable[Node[Any]], callback: Callable[..., object], name: str | None = None
    ) -> Node[object]: ...

    @overload
    def effect(
        self, deps: Iterable[Node[Any]], callback: None = None, name: str | None = None
    ) -> Callable[[Callable[..., object]], Node[object]]: ...

    def effect(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[..., object] | None = None,
        name: str | None = None,
    ) -> Node[object] | Callable[[Callable[..., object]], Node[object]]:
        """Create or decorate a value-level side effect over dependencies (Python
        sugar, with derived's gate); it sends nothing."""

        self._check_thread()
        dep_list = list(deps)
        if callback is None:
            return lambda fn: self.effect(dep_list, fn, name or fn.__name__)
        _reject_async_callable(callback)
        act = callback

        def run(ctx: _k.Ctx) -> None:
            values = _gate(ctx)
            if values is None:
                return
            _reject_awaitable(_guarded(lambda: act(*values)))

        return self._add_node(name, dep_list, run, callback, None)

    @overload
    def node(
        self,
        deps: Iterable[Node[Any]],
        callback: NodeCallback,
        name: str | None = None,
        *,
        complete: Policy = "auto",
        error: Policy = "auto",
        invalidate: Policy = "auto",
        quiet: bool = False,
        setup: SetupCallback | None = None,
        recipe: Recipe | None = None,
    ) -> Node[object]: ...

    @overload
    def node(
        self,
        deps: Iterable[Node[Any]],
        callback: None = None,
        name: str | None = None,
        *,
        complete: Policy = "auto",
        error: Policy = "auto",
        invalidate: Policy = "auto",
        quiet: bool = False,
        setup: SetupCallback | None = None,
        recipe: Recipe | None = None,
    ) -> Callable[[NodeCallback], Node[object]]: ...

    def node(
        self,
        deps: Iterable[Node[Any]],
        callback: NodeCallback | None = None,
        name: str | None = None,
        *,
        complete: Policy = "auto",
        error: Policy = "auto",
        invalidate: Policy = "auto",
        quiet: bool = False,
        setup: SetupCallback | None = None,
        recipe: Recipe | None = None,
    ) -> Node[object] | Callable[[NodeCallback], Node[object]]:
        """Create or decorate an advanced ``Ctx``-level node.

        Its callback runs on every wave it is given, with no first-run gate.
        ``complete``, ``error`` and ``invalidate`` choose each kind's auto or manual
        policy; a quiet node passes no pending mark on. ``setup(ctx)`` runs once per
        lifetime before the first run and may return the lifetime's cleanup. A
        ``recipe`` makes the node restorable.
        """

        self._check_thread()
        dep_list = list(deps)
        for label, policy in (("complete", complete), ("error", error), ("invalidate", invalidate)):
            if policy not in ("auto", "manual"):
                msg = f"{label} must be 'auto' or 'manual'"
                raise GraphReflyValueError(msg)
        _reject_non_bool("quiet", quiet)
        if recipe is not None and not isinstance(recipe, Recipe):
            msg = "recipe must be a graphrefly.Recipe"
            raise GraphReflyValueError(msg)
        if callback is None:
            return lambda fn: self.node(
                dep_list,
                fn,
                name or getattr(fn, "__name__", None),
                complete=complete,
                error=error,
                invalidate=invalidate,
                quiet=quiet,
                setup=setup,
                recipe=recipe,
            )
        _reject_async_callable(callback)
        if setup is not None:
            _reject_async_callable(setup)
        return self._add_node(
            name,
            dep_list,
            self._ctx_callback(callback),
            callback,
            setup,
            kernel_setup=self._ctx_setup(setup),
            complete=complete,
            error=error,
            invalidate=invalidate,
            quiet=quiet,
            recipe=recipe,
        )

    # ---- requests ----------------------------------------------------------------

    def batch(self, callback: Callable[[], R]) -> R:
        """Run ``callback`` at once and commit its host inputs as one wave.

        Nothing is delivered while it runs; it returns what ``callback`` returned. A
        callback that raises commits nothing. During a wave the commit waits until
        the wave ends.
        """

        self._check_thread()
        _reject_async_callable(callback)
        return self._call(lambda: self._kernel.batch(callback))

    def replace(
        self,
        node: Node[Any],
        deps: Iterable[Node[Any]],
        callback: NodeCallback,
        *,
        setup: SetupCallback | None = None,
        recipe: Recipe | None = None,
    ) -> None:
        """Give a computed node a new instance under the same name and subscribers."""

        self._check_thread()
        node_id = self._node_id(node)
        ids = [self._node_id(dep) for dep in deps]
        _reject_async_callable(callback)
        if recipe is not None and not isinstance(recipe, Recipe):
            msg = "recipe must be a graphrefly.Recipe"
            raise GraphReflyValueError(msg)
        fn = _guard_fn(self._ctx_callback(callback), callback)
        kernel_setup = _guard_setup(self._ctx_setup(setup), setup)
        kernel_recipe = None if recipe is None else {"ref": recipe.ref, "config": recipe.config}

        def run() -> None:
            self._kernel.replace(node_id, ids, fn, setup=kernel_setup, recipe=kernel_recipe)

        self._call(run)

    # ---- reads -----------------------------------------------------------------------

    def definition(self, node: Node[Any]) -> NodeDefinition:
        """Return the node's current definition; reading changes nothing."""

        self._check_thread()
        node_id = self._node_id(node)
        definition = self._kernel.definition(node_id)
        recipe = definition.recipe
        return NodeDefinition(
            tuple(self._nodes[dep] for dep in definition.deps),
            # The user's callback and setup ride on the instance the kernel holds, so a
            # replace a batch body dropped or the kernel refused leaves them as they are.
            getattr(definition.fn, _AUTHORED, None),
            getattr(definition.setup, _AUTHORED, None),
            None if not isinstance(recipe, dict) else Recipe(recipe["ref"], recipe.get("config")),
            definition.complete,
            definition.error,
            definition.invalidate,
            definition.quiet,
        )

    def find(self, name: str) -> Node[Any] | None:
        """Return the node named ``name``, or ``None``.

        A state node comes back as its ``State``, so it can be written after a lookup or
        a restore; narrow the result with ``isinstance(node, State)``.
        """

        self._check_thread()
        if not isinstance(name, str):
            msg = "a node name is a str"
            raise GraphReflyValueError(msg)
        return self._nodes.get(name)

    def describe(self) -> dict[str, Any]:
        """Return the topology as JSON-ready data: each node with its current deps."""

        self._check_thread()
        nodes: list[dict[str, Any]] = []
        for described in self._kernel.describe():
            held = self._kernel.get(described.id)
            entry: dict[str, Any] = {
                "name": described.id,
                "kind": described.kind,
                "deps": list(described.deps),
                "quiet": described.quiet,
                "has_value": held.has,
            }
            if held.has:
                entry["value"] = held.value
            nodes.append(entry)
        return {"name": self._name, "nodes": nodes}

    def observe(self, callback: Callable[[GraphEvent], object]) -> Subscription:
        """Observe every array any node publishes, without activating anything.

        A callback that raises goes to the graph's reporter as a
        ``SubscriberCallbackError``, as a subscriber's does.
        """

        self._check_thread()
        _reject_async_callable(callback)
        seq = [0]

        def observer(node_id: str, array: tuple[_k.Message, ...]) -> None:
            seq[0] += 1
            event = GraphEvent(node_id, cast("Array[Any]", array), seq[0])
            _observed(lambda: callback(event))

        remove = self._kernel.observe(observer)
        subscription = Subscription(
            remove,
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
        )
        self._lifetime.register(subscription)
        return subscription

    # ---- host lifetime --------------------------------------------------------------

    def retain(self, node: Node[T], *, reason: str | None = None) -> Retain:
        """Keep a node active until the returned retain token is released."""

        self._check_thread()
        if reason is not None and not isinstance(reason, str):
            msg = "retain reason must be a string when provided"
            raise GraphReflyValueError(msg)
        node_id = self._node_id(node)
        sink_id = self._new_sink_id()
        subscription = Subscription(
            lambda: self._kernel.unsubscribe(sink_id, node_id),
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
        )
        self._lifetime.register(subscription)
        self._call(lambda: self._kernel.subscribe(sink_id, node_id))
        retain = Retain(subscription, owner_thread=self._owner_thread, lifetime=self._lifetime)
        self._lifetime.register_retain(retain)
        return retain

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

    def checkpoint(self) -> GraphCheckpoint:
        """Capture this graph at a wave boundary as strict JSON (R-checkpoint)."""

        self._check_thread()
        try:
            return _checkpoint.capture(self._kernel)
        except _checkpoint.CheckpointError as error:
            raise GraphReflyCheckpointError(str(error)) from error

    # ---- internals ---------------------------------------------------------------------

    def _ctx_callback(self, callback: NodeCallback) -> Callable[[_k.Ctx], None]:
        def run(ctx: _k.Ctx) -> None:
            _reject_awaitable(_guarded(lambda: callback(Ctx(ctx, self))))

        return run

    def _ctx_setup(self, setup: SetupCallback | None) -> Callable[[_k.Ctx], object] | None:
        if setup is None:
            return None
        start = setup

        def run(ctx: _k.Ctx) -> object:
            cleanup = _guarded(lambda: start(Ctx(ctx, self)))
            _reject_awaitable(cleanup)
            if cleanup is None:
                return None
            if not callable(cleanup):
                msg = "a setup returns its lifetime's cleanup, a callable, or None"
                raise GraphCallbackError("GraphReflyValueError", msg)
            stop = cast("Callable[[], object]", cleanup)

            def close() -> None:
                _reject_awaitable(stop())

            return close

        return run

    def _add_node(
        self,
        name: str | None,
        deps: list[Node[Any]],
        fn: Callable[[_k.Ctx], None],
        callback: Callable[..., object] | None,
        setup: SetupCallback | None,
        *,
        kernel_setup: Callable[[_k.Ctx], object] | None = None,
        complete: Policy = "auto",
        error: Policy = "auto",
        invalidate: Policy = "auto",
        quiet: bool = False,
        recipe: Recipe | None = None,
    ) -> Node[Any]:
        ids = [self._node_id(dep) for dep in deps]
        node_id = self._new_id(name)
        kernel_recipe = None if recipe is None else {"ref": recipe.ref, "config": recipe.config}
        guarded = _guard_fn(fn, callback)
        guarded_setup = _guard_setup(kernel_setup, setup)

        def add() -> None:
            self._kernel.node(
                node_id,
                ids,
                guarded,
                complete=complete,
                error=error,
                invalidate=invalidate,
                quiet=quiet,
                setup=guarded_setup,
                recipe=kernel_recipe,
            )

        self._call(add)
        return self._register(Node(self, node_id))

    def _source(
        self,
        name: str | None,
        start: Callable[[_k.Ctx], Callable[[], None] | None],
    ) -> Node[Any]:
        """A dependency-free node whose setup starts host work at each activation and
        whose cleanup stops it; the work sends its results as later waves."""

        def run(ctx: _k.Ctx) -> None:
            return None

        return self._add_node(name, [], run, None, None, kernel_setup=start)

    def _register(self, node: Node[Any]) -> Node[Any]:
        self._nodes[node.name] = node
        return node

    def _new_id(self, name: str | None) -> str:
        if name is None:
            while True:
                self._next_id += 1
                node_id = f"#{self._next_id}"
                if node_id not in self._nodes:
                    return node_id
        if not isinstance(name, str) or not name or name.startswith("#"):
            msg = "a node name is a non-empty str that does not start with '#'"
            raise GraphReflyValueError(msg)
        if name in self._nodes:
            msg = f"node name {name!r} is already used in this graph"
            raise GraphReflyValueError(msg)
        return name

    def _new_sink_id(self) -> str:
        self._next_sink += 1
        return f"sink#{self._next_sink}"

    def _node_id(self, node: Node[Any]) -> str:
        if not isinstance(node, Node):
            msg = "dependencies must be graphrefly.Node objects"
            raise GraphReflyValueError(msg)
        if node._graph is not self:
            msg = "node must belong to this GraphReFly graph"
            raise GraphReflyRuntimeError(msg)
        return node._id

    def _call(self, op: Callable[[], R]) -> R:
        """Run a kernel host call, mapping its failures to the facade's taxonomy."""

        try:
            return op()
        except _k.KernelError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except _k.FATAL as error:
            _poison_on_fatal(self._lifetime, error)
        except Exception:
            raise
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def _on_report(self, report: _k.Report) -> None:
        if self._report_to is None:
            _LOGGER.error("graphrefly %s failed: %r", report.source, report.error)
            return
        _reject_awaitable(self._report_to(HostReport(report.source, report.error)))

    def _check_thread(self, *, allow_closed: bool = False) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python graphs are bound to their creating thread"
            raise GraphReflyRuntimeError(msg)
        if self._lifetime.is_closed() and not allow_closed:
            raise GraphReflyRuntimeError(self._lifetime.closed_message)


def _gate(ctx: _k.Ctx) -> list[object] | None:
    """derived's sugar: every dependency has a value and one sent DATA in this wave."""

    deps = ctx.deps
    fresh = False
    for dep in deps:
        if not dep.has:
            return None
        if not fresh and any(dep.waves):
            fresh = True
    if not fresh:
        return None
    return [dep.value for dep in deps]


def _guarded[V](call: Callable[[], V]) -> V:
    """Run a user callback; an Exception becomes a GraphCallbackError, which the kernel
    turns into the node's ERROR. A BaseException passes through untouched."""

    try:
        return call()
    except (GraphCallbackError, *_k.FATAL):
        raise
    except Exception as error:
        raise _callback_error(error) from error


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


def from_http(
    graph: Graph,
    request_or_url: HttpRequestLike,
    *,
    driver: LocalHttpDriver | None = None,
    runner: AsyncRunner | None = None,
    name: str | None = None,
) -> Node[HttpResponse]:
    """Create a source from one host-supplied HTTP request driver call per activation."""

    request = _normalize_http_request(request_or_url)
    _validate_network_source_args(
        graph, runner, driver, driver_method="request", driver_name="LocalHttpDriver"
    )
    assert runner is not None
    assert driver is not None
    http = driver
    reentry_queue = _runner_reentry_queue(graph, runner)

    def body(gate: _AsyncCompletionGate | _QueuedCompletionGate, job: _AsyncJob) -> AsyncJobFactory:
        async def run() -> None:
            try:
                if not job.active:
                    return
                response = http.request(request)
                if not isawaitable(response):
                    msg = "LocalHttpDriver.request(request) must return an awaitable"
                    raise GraphReflyRuntimeError(msg)
                gate.resolve(_normalize_http_response(await response), job.can_apply_completion)
            except BaseException as error:
                _settle_async_error(gate, job, error)
            finally:
                job.finish()

        return run

    return cast(
        "Node[HttpResponse]",
        graph._source(name, lambda ctx: _start_async(graph, ctx, runner, reentry_queue, body)),
    )


def from_sse(
    graph: Graph,
    request_or_url: HttpRequestLike,
    *,
    stream_driver: LocalHttpStreamDriver | None = None,
    runner: AsyncRunner | None = None,
    name: str | None = None,
) -> Node[SseEvent]:
    """Create an SSE source over a host-supplied HTTP byte-stream driver."""

    request = _sse_request_to_http_stream_request(_normalize_http_request(request_or_url))
    _validate_network_source_args(
        graph, runner, stream_driver, driver_method="stream", driver_name="LocalHttpStreamDriver"
    )
    assert runner is not None
    assert stream_driver is not None
    streams = stream_driver
    reentry_queue = _runner_reentry_queue(graph, runner)

    def body(gate: _AsyncCompletionGate | _QueuedCompletionGate, job: _AsyncJob) -> AsyncJobFactory:
        async def run() -> None:
            parser = _SseParser()
            saw_head = False
            stream_iter: object | None = None
            try:
                if not job.active:
                    return
                stream = streams.stream(request)
                if not hasattr(stream, "__aiter__"):
                    msg = "LocalHttpStreamDriver.stream(request) must return an async iterable"
                    raise GraphReflyRuntimeError(msg)
                stream_iter = stream.__aiter__()
                try:
                    async for raw_event in cast("AsyncIterable[object]", stream_iter):
                        if not job.active:
                            return
                        event = _normalize_http_stream_event(raw_event)
                        if isinstance(event, HttpStreamHeadEvent):
                            if saw_head:
                                raise GraphReflyRuntimeError(
                                    "from_sse: http stream emitted duplicate response head"
                                )
                            saw_head = True
                            _validate_sse_head(event.head)
                            continue
                        if isinstance(event, HttpStreamChunkEvent):
                            if len(event.chunk) == 0:
                                continue
                            if not saw_head:
                                raise GraphReflyRuntimeError(
                                    "from_sse: http stream chunk arrived before response head"
                                )
                            if len(event.chunk) > _SSE_PARSER_MAX_BUFFER_BYTES:
                                raise _SseParserError("from_sse: parser overflow", [])
                            try:
                                parsed_events = parser.push(event.chunk)
                            except _SseParserError as error:
                                for sse_event in error.events:
                                    gate.emit(sse_event, job.can_apply_completion)
                                raise error
                            for sse_event in parsed_events:
                                gate.emit(sse_event, job.can_apply_completion)
                            continue
                        if isinstance(event, HttpStreamErrorEvent):
                            if job.active:
                                gate.error(event.error, job.can_apply_completion)
                            return
                        if not saw_head:
                            raise GraphReflyRuntimeError(
                                "from_sse: http stream completed before response head"
                            )
                        try:
                            parsed_events = parser.complete()
                        except _SseParserError as error:
                            for sse_event in error.events:
                                gate.emit(sse_event, job.can_apply_completion)
                            raise error
                        for sse_event in parsed_events:
                            gate.emit(sse_event, job.can_apply_completion)
                        gate.complete(job.can_apply_completion)
                        return
                finally:
                    await _close_async_stream(stream_iter)
                raise GraphReflyRuntimeError("from_sse: http stream ended without Complete")
            except BaseException as error:
                _settle_async_error(gate, job, error)
            finally:
                job.finish()

        return run

    return cast(
        "Node[SseEvent]",
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


def version() -> str:
    """Return the installed Python package version."""

    return _VERSION


def restore_graph(
    checkpoint: GraphCheckpoint,
    *,
    registry: Mapping[str, RecipeFactory],
    name: str | None = None,
    report: Callable[[HostReport], object] | None = None,
) -> Graph:
    """Build a new graph from ``checkpoint`` (R-checkpoint); no callback runs.

    ``registry`` maps each recipe's ``ref`` to a factory called as
    ``factory(config, RecipeContext(graph, name, make))``, returning the node callback or
    a ``(callback, setup)`` pair; ``make`` builds another registered recipe. State
    nodes need no recipe. A node built without a recipe is local-only, and restoring it
    fails, as do a missing recipe and a checkpoint in another format.
    """

    if not isinstance(registry, Mapping):
        msg = "registry must map recipe refs to factories"
        raise GraphReflyValueError(msg)
    graph = Graph(name, report=report)

    def build(
        ref: str, config: object, node_name: str
    ) -> tuple[NodeCallback, SetupCallback | None]:
        factory = registry.get(ref)
        if factory is None:
            raise GraphReflyRestoreError(f"restore: no recipe {ref}")
        made = factory(config, RecipeContext(graph, node_name, build))
        callback, setup = made if isinstance(made, tuple) else (made, None)
        if not callable(callback) or (setup is not None and not callable(setup)):
            msg = f"recipe {ref} must give a callback, or a (callback, setup) pair"
            raise GraphReflyRestoreError(msg)
        return callback, setup

    def adapt(ref: str) -> _checkpoint.Factory:
        def make(config: object, at: _checkpoint.RecipeContext) -> _checkpoint.Built:
            callback, setup = build(ref, config, at.id)
            return _checkpoint.Built(
                _guard_fn(graph._ctx_callback(callback), callback),
                _guard_setup(graph._ctx_setup(setup), setup),
            )

        return make

    adapted = {ref: adapt(ref) for ref in registry}
    try:
        _checkpoint.restore(checkpoint, adapted, into=graph._kernel)
    except _checkpoint.CheckpointError as error:
        raise GraphReflyRestoreError(str(error)) from error
    for described in graph._kernel.describe():
        handle: Node[Any] = (
            State(graph, described.id) if described.kind == "state" else Node(graph, described.id)
        )
        graph._register(handle)
    return graph


def _normalize_http_request(request_or_url: HttpRequestLike) -> HttpRequest:
    if isinstance(request_or_url, str):
        if request_or_url == "":
            raise GraphReflyValueError("HTTP request url must be a non-empty str")
        return HttpRequest(method="GET", url=request_or_url)
    if not isinstance(request_or_url, HttpRequest):
        msg = "HTTP request must be a URL str or graphrefly.HttpRequest"
        raise GraphReflyValueError(msg)
    method = _normalize_non_empty_str("HTTP request method", request_or_url.method)
    url = _normalize_non_empty_str("HTTP request url", request_or_url.url)
    return HttpRequest(
        method=method,
        url=url,
        headers=_normalize_headers(request_or_url.headers),
        body=_normalize_bytes("HTTP request body", request_or_url.body),
    )


def _normalize_http_response(response: object) -> HttpResponse:
    if not isinstance(response, HttpResponse):
        msg = "LocalHttpDriver.request(request) must resolve to graphrefly.HttpResponse"
        raise GraphReflyRuntimeError(msg)
    status = _normalize_status("HTTP response status", response.status)
    return HttpResponse(
        status=status,
        headers=_normalize_headers(response.headers),
        body=_normalize_bytes("HTTP response body", response.body),
    )


def _normalize_http_stream_event(event: object) -> HttpStreamDriverEvent:
    if isinstance(event, HttpStreamHeadEvent):
        return HttpStreamHeadEvent(_normalize_http_stream_head(event.head))
    if isinstance(event, HttpStreamHead):
        return HttpStreamHeadEvent(_normalize_http_stream_head(event))
    if isinstance(event, HttpStreamChunkEvent):
        return HttpStreamChunkEvent(_normalize_bytes("HTTP stream chunk", event.chunk))
    if isinstance(event, HttpStreamErrorEvent):
        if not isinstance(event.error, BaseException):
            msg = "HTTP stream Error event must carry a BaseException"
            raise GraphReflyRuntimeError(msg)
        return event
    if isinstance(event, HttpStreamCompleteEvent):
        return event
    msg = "HTTP stream driver events must be Head, Chunk, Error, or Complete"
    raise GraphReflyRuntimeError(msg)


def _normalize_http_stream_head(head: object) -> HttpStreamHead:
    if not isinstance(head, HttpStreamHead):
        msg = "HTTP stream Head event must carry graphrefly.HttpStreamHead"
        raise GraphReflyRuntimeError(msg)
    return HttpStreamHead(
        status=_normalize_status("HTTP stream status", head.status),
        headers=_normalize_headers(head.headers),
    )


def _sse_request_to_http_stream_request(request: HttpRequest) -> HttpRequest:
    headers = list(request.headers)
    if not any(key.lower() == "accept" for key, _value in headers):
        headers.append(("Accept", "text/event-stream"))
    return HttpRequest(
        method="GET",
        url=request.url,
        headers=tuple(headers),
        body=b"",
    )


def _validate_sse_head(head: HttpStreamHead) -> None:
    if not 200 <= head.status <= 299:
        raise GraphReflyRuntimeError(f"from_sse: unacceptable http status {head.status}")
    content_type = next(
        (value for key, value in head.headers if key.lower() == "content-type"),
        None,
    )
    if content_type is None:
        raise GraphReflyRuntimeError("from_sse: missing text/event-stream content-type")
    media_type = content_type.split(";", 1)[0].strip()
    if media_type.lower() != "text/event-stream":
        raise GraphReflyRuntimeError(f"from_sse: unacceptable content-type {content_type}")


def _normalize_headers(headers: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    normalized: list[tuple[str, str]] = []
    for item in headers:
        if not isinstance(item, tuple) or len(item) != 2:
            msg = "HTTP headers must be (name, value) string pairs"
            raise GraphReflyValueError(msg)
        key, value = item
        if not isinstance(key, str) or key == "":
            raise GraphReflyValueError("HTTP header names must be non-empty strings")
        if not isinstance(value, str):
            raise GraphReflyValueError("HTTP header values must be strings")
        normalized.append((key, value))
    return tuple(normalized)


def _normalize_bytes(label: str, value: object) -> bytes:
    if isinstance(value, bytes):
        return bytes(value)
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    raise GraphReflyValueError(f"{label} must be bytes")


def _normalize_non_empty_str(label: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise GraphReflyValueError(f"{label} must be a non-empty str")
    return value


def _normalize_status(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 100 or value > 999:
        raise GraphReflyValueError(f"{label} must be an HTTP status int")
    return value


def _validate_network_source_args(
    graph: Graph,
    runner: AsyncRunner | None,
    driver: object | None,
    *,
    driver_method: str,
    driver_name: str,
) -> None:
    graph._check_thread()
    if runner is None:
        msg = "network source adapters require an explicit AsyncRunner"
        raise GraphReflyValueError(msg)
    _validate_runner(runner)
    if driver is None:
        raise GraphReflyValueError(f"{driver_name} is required")
    if not callable(getattr(driver, driver_method, None)):
        raise GraphReflyValueError(f"{driver_name} must provide {driver_method}(request)")


class _SseParserError(GraphReflyRuntimeError):
    def __init__(self, message: str, events: list[SseEvent]) -> None:
        self.events = events
        super().__init__(message)


class _SseParser:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._line = ""
        self._line_bytes = 0
        self._drop_next_lf = False
        self._event: str | None = None
        self._data_lines: list[str] = []
        self._data_bytes = 0
        self._id: str | None = None
        self._retry_ms: int | None = None

    def push(self, chunk: bytes) -> list[SseEvent]:
        try:
            text = self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError as error:
            raise _SseParserError("from_sse: invalid utf-8 in event stream", []) from error
        return self._push_text(text)

    def complete(self) -> list[SseEvent]:
        events: list[SseEvent] = []
        try:
            tail = self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as error:
            raise _SseParserError("from_sse: invalid utf-8 in event stream", events) from error
        events.extend(self._push_text(tail))
        if self._line:
            try:
                self._finish_line_into(events)
            except _SseParserError as error:
                raise _SseParserError(str(error), events + error.events) from error
        dispatched = self._dispatch_event()
        if dispatched is not None:
            events.append(dispatched)
        return events

    def _push_text(self, text: str) -> list[SseEvent]:
        events: list[SseEvent] = []
        for char in text:
            if self._drop_next_lf:
                self._drop_next_lf = False
                if char == "\n":
                    continue
            if char == "\r":
                try:
                    self._finish_line_into(events)
                except _SseParserError as error:
                    raise _SseParserError(str(error), events + error.events) from error
                self._drop_next_lf = True
            elif char == "\n":
                try:
                    self._finish_line_into(events)
                except _SseParserError as error:
                    raise _SseParserError(str(error), events + error.events) from error
            else:
                self._line += char
                self._line_bytes += len(char.encode("utf-8"))
                if self._line_bytes > _SSE_PARSER_MAX_BUFFER_BYTES:
                    raise _SseParserError("from_sse: parser overflow", events)
        return events

    def _finish_line_into(self, events: list[SseEvent]) -> None:
        event = self._process_line(self._line)
        self._line = ""
        self._line_bytes = 0
        if event is not None:
            events.append(event)

    def _process_line(self, line: str) -> SseEvent | None:
        if line == "":
            return self._dispatch_event()
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            added = len(value.encode("utf-8")) + (1 if self._data_lines else 0)
            if self._data_bytes + added > _SSE_PARSER_MAX_BUFFER_BYTES:
                raise _SseParserError("from_sse: parser overflow", [])
            self._data_bytes += added
            self._data_lines.append(value)
        elif field == "event":
            self._event = value
        elif field == "id":
            self._id = value
        elif field == "retry" and value.isdecimal() and len(value) <= 20:
            retry_ms = int(value)
            if retry_ms <= _SSE_RETRY_MAX:
                self._retry_ms = retry_ms
        return None

    def _dispatch_event(self) -> SseEvent | None:
        if not self._data_lines:
            self._clear_event()
            return None
        event = SseEvent(
            event=self._event,
            data="\n".join(self._data_lines),
            id=self._id,
            retry_ms=self._retry_ms,
        )
        self._clear_event()
        return event

    def _clear_event(self) -> None:
        self._event = None
        self._data_lines = []
        self._data_bytes = 0
        self._id = None
        self._retry_ms = None


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


def _poison_on_fatal(lifetime: _GraphLifetime, error: BaseException) -> NoReturn:
    if not isinstance(error, Exception) or isinstance(error, _k.FATAL):
        lifetime.poison()
    raise error


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


def _callback_error(error: BaseException) -> GraphCallbackError:
    if isinstance(error, GraphCallbackError):
        return error
    return GraphCallbackError(type(error).__name__, str(error), raw=error)


def _guard_fn(
    fn: Callable[[_k.Ctx], None], authored: Callable[..., object] | None = None
) -> Callable[[_k.Ctx], None]:
    """A node function whose Exception becomes a GraphCallbackError, the node's ERROR.

    It carries the user's callback, which ``Graph.definition`` reads from the kernel.
    """

    def run(ctx: _k.Ctx) -> None:
        try:
            fn(ctx)
        except (GraphCallbackError, *_k.FATAL):
            raise
        except Exception as error:
            raise _callback_error(error) from error

    setattr(run, _AUTHORED, authored)
    return run


def _guard_setup(
    setup: Callable[[_k.Ctx], object] | None, authored: SetupCallback | None = None
) -> Callable[[_k.Ctx], object] | None:
    if setup is None:
        return None
    start = setup

    def run(ctx: _k.Ctx) -> object:
        try:
            return start(ctx)
        except (GraphCallbackError, *_k.FATAL):
            raise
        except Exception as error:
            raise _callback_error(error) from error

    setattr(run, _AUTHORED, authored)
    return run


def _observed(call: Callable[[], object]) -> None:
    """Run a subscribe or observe callback; its Exception goes on to the kernel, which
    reports it, as a SubscriberCallbackError (graphrefly:D428). Nothing keeps it."""

    try:
        _reject_awaitable(call())
    except (SubscriberCallbackError, *_k.FATAL):
        raise
    except Exception as error:
        raise SubscriberCallbackError(error) from error


def _source_tree_version() -> str | None:
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    version_value = project.get("version")
    return version_value if isinstance(version_value, str) else None


def _package_version() -> str:
    try:
        return metadata.version("graphrefly")
    except PackageNotFoundError:
        return _source_tree_version() or _VERSION


_VERSION = _package_version()
