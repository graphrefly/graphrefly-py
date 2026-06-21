"""Typed Python facade over the private PyO3 foundation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import metadata
from importlib.metadata import PackageNotFoundError
from inspect import isawaitable, iscoroutinefunction
from threading import get_ident
from typing import Any, TypeVar, cast

from graphrefly import _native
from graphrefly.exceptions import CallbackError, GraphReflyRuntimeError, GraphReflyValueError

T = TypeVar("T")
U = TypeVar("U")
_VERSION = "0.21.0a0"


@dataclass(frozen=True, slots=True)
class Message:
    """A public subscription event.

    `value` is present for DATA and ERROR observations. Other protocol messages use
    `None` as the v0 no-DATA sentinel; domain DATA may not be `None` in this slice.
    """

    kind: str
    value: object | None = None


@dataclass(frozen=True, slots=True)
class GraphEvent:
    """Graph-level observation from the native engine."""

    path: str
    message: Message
    seq: int


class Subscription:
    """Idempotent subscription handle."""

    def __init__(self, native: _native.Subscription, *, owner_thread: int) -> None:
        self._native = native
        self._owner_thread = owner_thread
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

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
        return cast("T | None", self._native.cache())

    @property
    def status(self) -> str:
        self._check_thread()
        return self._native.status()

    def subscribe(self, callback: Callable[[Message], object]) -> Subscription:
        self._check_thread()
        _reject_async_callable(callback)

        def native_callback(kind: str, value: object) -> None:
            result = callback(Message(kind=kind, value=value))
            _reject_awaitable(result)

        try:
            return Subscription(
                self._native.subscribe(native_callback),
                owner_thread=self._owner_thread,
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

    def producer(self, callback: Callable[[], T], name: str | None = None) -> Node[T]:
        self._check_thread()

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

    def derived(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[..., U],
        name: str | None = None,
    ) -> Node[U]:
        self._check_thread()
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

    def effect(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[..., object],
        name: str | None = None,
    ) -> Node[object]:
        self._check_thread()
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

    def observe(self, callback: Callable[[GraphEvent], object]) -> Subscription:
        self._check_thread()
        _reject_async_callable(callback)

        def native_callback(path: str, kind: str, value: object, seq: int) -> None:
            event = GraphEvent(path=path, message=Message(kind=kind, value=value), seq=seq)
            result = callback(event)
            _reject_awaitable(result)

        return Subscription(self._native.observe(native_callback), owner_thread=self._owner_thread)

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


def _normalize_describe(snapshot: dict[str, Any]) -> dict[str, Any]:
    nodes = []
    for node in snapshot.get("nodes", []):
        normalized = dict(node)
        has_value = normalized.get("value") is not None
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
