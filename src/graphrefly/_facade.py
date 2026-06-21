"""Typed Python facade over the private PyO3 foundation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import metadata
from importlib.metadata import PackageNotFoundError
from inspect import isawaitable, iscoroutinefunction
from threading import get_ident
from typing import Any, Literal, TypeVar, cast, overload

from graphrefly import _native
from graphrefly.exceptions import (
    CallbackError,
    GraphCallbackError,
    GraphReflyRuntimeError,
    GraphReflyValueError,
    SubscriberCallbackError,
)

T = TypeVar("T")
U = TypeVar("U")
_VERSION = "0.21.0a0"
_MAX_CALLBACK_ERRORS = 32


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
        callback_errors: list[SubscriberCallbackError] | None = None,
    ) -> None:
        self._native = native
        self._owner_thread = owner_thread
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
            self._native.unsubscribe()
            self._closed = True

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.unsubscribe()

    def _check_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python subscriptions are bound to their creating thread in v0"
            raise GraphReflyRuntimeError(msg)


class Node[T]:
    """A typed Python handle around an opaque native node."""

    def __init__(
        self,
        native: _native.Node,
        *,
        owner_thread: int,
        writable: bool = False,
    ) -> None:
        self._native = native
        self._owner_thread = owner_thread
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

    def cache(self) -> T | None:
        self._check_thread()
        has_value, value = self._native.cache_entry()
        if not has_value:
            return None
        return cast("T | None", value)

    @property
    def status(self) -> str:
        self._check_thread()
        return self._native.status()

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
            except BaseException as error:
                _record_observer_error(error, callback_errors, on_error)

        try:
            return Subscription(
                self._native.subscribe(native_callback),
                owner_thread=self._owner_thread,
                callback_errors=callback_errors,
            )
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error

    def _check_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python nodes are bound to their creating thread in v0"
            raise GraphReflyRuntimeError(msg)


class Graph:
    """Graph-first authoring facade over the native Rust engine."""

    def __init__(self, name: str | None = None) -> None:
        self._owner_thread = get_ident()
        self._native = _native.Graph(name)

    def __enter__(self) -> Graph:
        self._check_thread()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._check_thread()

    def state(self, value: T, name: str | None = None) -> Node[T]:
        self._check_thread()
        try:
            return Node(
                self._native.state(value, name),
                owner_thread=self._owner_thread,
                writable=True,
            )
        except ValueError as error:
            raise GraphReflyValueError(str(error)) from error
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error

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
            )
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error

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
            )
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error

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
            )
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error

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

    def describe(self) -> dict[str, Any]:
        self._check_thread()
        return _normalize_describe(self._native.describe())

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
            except BaseException as error:
                _record_observer_error(error, callback_errors, on_error)

        return Subscription(
            self._native.observe(native_callback),
            owner_thread=self._owner_thread,
            callback_errors=callback_errors,
        )

    def _native_deps(self, deps: Iterable[Node[Any]]) -> list[_native.Node]:
        native_deps = []
        for dep in deps:
            dep._check_thread()
            if dep._owner_thread != self._owner_thread:
                msg = "dependency nodes must belong to the same GraphReFly Python thread in v0"
                raise GraphReflyRuntimeError(msg)
            native_deps.append(dep._native)
        return native_deps

    def _check_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = "GraphReFly Python graphs are bound to their creating thread in v0"
            raise GraphReflyRuntimeError(msg)


def version() -> str:
    """Return the installed Python package version."""

    try:
        return metadata.version("graphrefly")
    except PackageNotFoundError:
        return _VERSION


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
    error: BaseException,
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
        except BaseException as handler_error:
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


def _scrub_exception(error: BaseException) -> BaseException:
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
