"""Typed Python facade over the private PyO3 foundation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import metadata
from importlib.metadata import PackageNotFoundError
from inspect import isawaitable, iscoroutinefunction
from threading import get_ident
from typing import Any, Literal, NoReturn, TypeVar, cast, overload
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


class _GraphLifetime:
    def __init__(self, owner_thread: int) -> None:
        self.owner_thread = owner_thread
        self.closed = False
        self.poisoned = False
        self.subscriptions: list[ReferenceType[Subscription]] = []

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

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        subscriptions = tuple(item() for item in self.subscriptions)
        self.subscriptions.clear()
        for subscription in subscriptions:
            if subscription is not None:
                subscription._close_from_graph()

    def poison(self) -> None:
        self.poisoned = True
        self.close()


@dataclass(frozen=True, slots=True)
class GraphEvent:
    """Graph-level observation from the native engine."""

    path: str
    message: Message[Any]
    tier: int
    seq: int


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

    def emit(self, value: object) -> None:
        self._check_thread()
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

    def _check_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python ctx objects are bound to their creating thread in v0"
            raise GraphReflyRuntimeError(msg)
        if self._lifetime.closed:
            raise GraphReflyRuntimeError(self._lifetime.closed_message)


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

        def native_callback() -> T:
            value = callback()
            _reject_awaitable(value)
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
        native_deps = self._native_deps(deps)

        def native_callback(*values: object) -> U:
            value = callback(*values)
            _reject_awaitable(value)
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
    ) -> Node[object]: ...

    @overload
    def node(
        self,
        deps: Iterable[Node[Any]],
        callback: None = None,
        name: str | None = None,
    ) -> Callable[[Callable[[Ctx], object]], Node[object]]: ...

    def node(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[[Ctx], object] | None = None,
        name: str | None = None,
    ) -> Node[object] | Callable[[Callable[[Ctx], object]], Node[object]]:
        self._check_thread()
        deps = list(deps)
        if callback is None:
            return lambda fn: self.node(deps, fn, name or fn.__name__)
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
                self._native.node(native_deps, native_callback, name),
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


def version() -> str:
    """Return the installed Python package version."""

    try:
        return metadata.version("graphrefly")
    except PackageNotFoundError:
        return _VERSION


def _poison_on_fatal(lifetime: _GraphLifetime, error: BaseException) -> NoReturn:
    if not isinstance(error, Exception):
        lifetime.poison()
    raise error


def _reject_non_string_lock_id(lock_id: str) -> None:
    if not isinstance(lock_id, str):
        msg = "pause/resume lock_id must be a str in the Python facade"
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
