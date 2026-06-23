"""Typed Python facade over the private PyO3 foundation."""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from dataclasses import dataclass
from importlib import metadata
from importlib.metadata import PackageNotFoundError
from inspect import isawaitable, iscoroutine, iscoroutinefunction
from threading import get_ident
from typing import Any, ClassVar, Final, Literal, NoReturn, Protocol, TypeVar, cast, overload
from weakref import ReferenceType, ref

from graphrefly import _native
from graphrefly.exceptions import (
    CallbackError,
    GraphCallbackError,
    GraphReflyNoDataError,
    GraphReflyRuntimeError,
    GraphReflyValueError,
    SubscriberCallbackError,
)

T = TypeVar("T")
U = TypeVar("U")
_VERSION = "0.21.0a0"
_MAX_CALLBACK_ERRORS = 32
_NO_DEFAULT = object()


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


class AsyncRunner(Protocol):
    """Framework-neutral async job runner supplied explicitly by the host."""

    def spawn(self, job: AsyncJobFactory) -> object: ...


class _GraphLifetime:
    def __init__(self, owner_thread: int) -> None:
        self.owner_thread = owner_thread
        self.closed = False
        self.poisoned = False
        self.subscriptions: list[ReferenceType[Subscription]] = []
        self.async_jobs: list[ReferenceType[_AsyncJob]] = []

    @property
    def closed_message(self) -> str:
        if self.poisoned:
            return "GraphReFly graph is closed after a fatal host boundary abort"
        return "GraphReFly graph is closed"

    def register(self, subscription: Subscription) -> None:
        if self.closed:
            subscription._close_from_graph()
            raise GraphReflyRuntimeError(self.closed_message)
        self.subscriptions.append(ref(subscription))

    def unregister(self, subscription: Subscription) -> None:
        self.subscriptions = [
            item
            for item in self.subscriptions
            if (live := item()) is not None and live is not subscription
        ]

    def register_async_job(self, job: _AsyncJob) -> None:
        if self.closed:
            job.cancel()
            return
        self.async_jobs.append(ref(job))

    def unregister_async_job(self, job: _AsyncJob) -> None:
        self.async_jobs = [
            item for item in self.async_jobs if (live := item()) is not None and live is not job
        ]

    def close(self, *, suppress_errors: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        self._cancel_async_jobs()
        subscriptions = tuple(item() for item in self.subscriptions)
        self.subscriptions.clear()
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
        self.poisoned = True
        self.close(suppress_errors=True)

    def _cancel_async_jobs(self) -> None:
        jobs = tuple(item() for item in self.async_jobs)
        self.async_jobs.clear()
        for job in jobs:
            if job is not None:
                job.cancel()


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
    ) -> None:
        self._native = native
        self._owner_thread = owner_thread
        self._lifetime = lifetime

    def register_deactivation(self, callback: Callable[[], object]) -> None:
        self._native.on_deactivation(callback)

    def emit(self, value: object) -> bool:
        if not self._can_reenter():
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

    def complete(self) -> bool:
        if not self._can_reenter():
            return False
        try:
            self._native.complete()
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)
        return True

    def resolve(self, value: object) -> bool:
        if not self._can_reenter():
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

    def error(self, error: BaseException) -> bool:
        if not self._can_reenter():
            return False
        try:
            self._native.error(_format_async_error(error))
        except RuntimeError as native_error:
            raise GraphReflyRuntimeError(str(native_error)) from native_error
        except BaseException as native_error:
            _poison_on_fatal(self._lifetime, native_error)
        return True

    def _can_reenter(self) -> bool:
        if get_ident() != self._owner_thread:
            msg = "async runner completion must re-enter GraphReFly on the graph owner thread"
            raise GraphReflyRuntimeError(msg)
        if self._lifetime.closed:
            return False
        try:
            return bool(self._native.is_live())
        except RuntimeError:
            return False
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)


class _AsyncJob:
    def __init__(self, runner: AsyncRunner, lifetime: _GraphLifetime) -> None:
        self._runner = runner
        self._lifetime = lifetime
        self._task: object | None = None
        self._cancel_requested = False
        self._done = False
        lifetime.register_async_job(self)

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    @property
    def active(self) -> bool:
        return not self._done and not self._cancel_requested and not self._lifetime.closed

    def start(self, body: AsyncJobFactory) -> None:
        try:
            task = _spawn_runner_job(self._runner, body)
        except Exception:
            self.finish()
            raise
        self._task = task
        if self._cancel_requested or self._done or self._lifetime.closed:
            _cancel_runner_task(self._runner, task)
            self.finish()

    def cancel(self) -> None:
        if self._done:
            return
        self._cancel_requested = True
        _cancel_runner_task(self._runner, self._task)
        self.finish()

    def finish(self) -> None:
        if not self._done:
            self._done = True
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


class Graph:
    """Graph-first authoring facade over the native Rust engine."""

    def __init__(self, name: str | None = None) -> None:
        self._owner_thread = get_ident()
        self._lifetime = _GraphLifetime(self._owner_thread)
        self._native = _native.Graph(name)

    def __enter__(self) -> Graph:
        self._check_thread()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        self._check_thread(allow_closed=True)
        return self._lifetime.closed

    def close(self) -> None:
        self._check_thread(allow_closed=True)
        try:
            self._native.raise_pending_fatal()
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)
        finally:
            self._lifetime.close()

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
                ),
                owner_thread=self._owner_thread,
                lifetime=self._lifetime,
            )
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

    def _check_thread(self, *, allow_closed: bool = False) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python graphs are bound to their creating thread in v0"
            raise GraphReflyRuntimeError(msg)
        if self._lifetime.closed and not allow_closed:
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

    def activate(native_ctx: Any) -> None:
        gate = _AsyncCompletionGate(
            native_ctx,
            owner_thread=graph._owner_thread,
            lifetime=graph._lifetime,
        )
        job = _AsyncJob(runner, graph._lifetime)
        gate.register_deactivation(job.cancel)

        async def run() -> None:
            try:
                if not job.active:
                    return
                awaitable = factory()
                if not isawaitable(awaitable):
                    msg = "from_awaitable factory must return an awaitable"
                    raise GraphReflyRuntimeError(msg)
                value = await awaitable
                if job.active:
                    gate.resolve(value)
            except Exception as error:
                if job.active:
                    gate.error(error)
            except BaseException as error:
                if not (job.cancel_requested or _is_cancellation_error(error)):
                    raise
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

    def activate(native_ctx: Any) -> None:
        gate = _AsyncCompletionGate(
            native_ctx,
            owner_thread=graph._owner_thread,
            lifetime=graph._lifetime,
        )
        job = _AsyncJob(runner, graph._lifetime)
        gate.register_deactivation(job.cancel)

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
                    gate.emit(value)
                if job.active:
                    gate.complete()
            except Exception as error:
                if job.active:
                    gate.error(error)
            except BaseException as error:
                if not (job.cancel_requested or _is_cancellation_error(error)):
                    raise
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
    generation = 0

    def activate(native_ctx: Any, *values: object) -> None:
        nonlocal generation
        generation += 1
        invocation_generation = generation
        gate = _AsyncCompletionGate(
            native_ctx,
            owner_thread=graph._owner_thread,
            lifetime=graph._lifetime,
        )
        job = _AsyncJob(runner, graph._lifetime)
        gate.register_deactivation(job.cancel)

        def is_current() -> bool:
            return invocation_generation == generation and job.active

        async def run() -> None:
            try:
                if not job.active:
                    return
                awaitable = callback(*values)
                if not isawaitable(awaitable):
                    msg = "async_node callback must return an awaitable"
                    raise GraphReflyRuntimeError(msg)
                value = await awaitable
                if is_current():
                    gate.emit(value)
            except Exception as error:
                if is_current():
                    gate.error(error)
            except BaseException as error:
                if not (job.cancel_requested or _is_cancellation_error(error)):
                    raise
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


def version() -> str:
    """Return the installed Python package version."""

    try:
        return metadata.version("graphrefly")
    except PackageNotFoundError:
        return _VERSION


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
