"""Typed Python facade over the Python GraphReFly kernel (graphrefly-py:D2, D4)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from importlib.metadata import PackageNotFoundError
from inspect import isawaitable, iscoroutinefunction
from threading import Lock, get_ident
from typing import TYPE_CHECKING, Any, Final, Literal, NoReturn, TypeVar, cast, overload
from weakref import ReferenceType, ref

from graphrefly import _checkpoint
from graphrefly import _kernel as _k
from graphrefly._kernel.graph import BOUNDARY_NAME_ERROR, NAME_ERROR
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

if TYPE_CHECKING:
    from graphrefly._async import GraphReentryQueue, _AsyncJob


T = TypeVar("T")


U = TypeVar("U")


R = TypeVar("R")


_VERSION = "0.23.0"  # x-release-please-version


_NO_DEFAULT = object()


_ABSENT: Any = object()


_LOGGER = logging.getLogger("graphrefly")


_AUTHORED = "_graphrefly_authored"


START: Final = "START"


PENDING: Final = "PENDING"


DATA: Final = "DATA"


RESOLVED: Final = "RESOLVED"


INVALIDATE: Final = "INVALIDATE"


COMPLETE: Final = "COMPLETE"


ERROR: Final = "ERROR"


type DataMessage[T] = tuple[Literal["DATA"], T]


type ErrorMessage = tuple[Literal["ERROR"], object]


type ControlMessage = tuple[Literal["START", "PENDING", "RESOLVED", "INVALIDATE", "COMPLETE"]]


type Message[T] = DataMessage[T] | ErrorMessage | ControlMessage


type Array[T] = tuple[Message[T], ...]


type NodeCallback = Callable[["Ctx"], object]


type SetupCallback = Callable[["Ctx"], object]


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


def _close_lifecycle_resource(resource: object) -> None:
    close = getattr(resource, "_close_from_graph", None)
    if callable(close):
        close()


class _GraphLifetime:
    def __init__(self, owner_thread: int) -> None:
        self.owner_thread = owner_thread
        self._lock = Lock()
        self.closed = False
        self.poisoned = False
        self.subscriptions: list[Subscription] = []
        self.async_jobs: list[ReferenceType[_AsyncJob]] = []
        self.reentry_queues: list[ReferenceType[GraphReentryQueue]] = []
        self.resources: list[object] = []  # the wire bridges it closes

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
            if not self.closed:
                if all(item is not resource for item in self.resources):
                    self.resources.append(resource)
                return
        _close_lifecycle_resource(resource)
        raise GraphReflyRuntimeError(self.closed_message)

    def close(self, *, suppress_errors: bool = False) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            queues = tuple(item() for item in self.reentry_queues)
            jobs = tuple(item() for item in self.async_jobs)
            subscriptions = tuple(self.subscriptions)
            resources = tuple(reversed(self.resources))
            self.resources.clear()
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
        for resource in resources:
            try:
                _close_lifecycle_resource(resource)
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
    """One array a node delivered, as ``Graph.observe`` hands it on: the node's ``id``
    and the array's messages, ``msgs`` (R-graph-surface)."""

    id: str
    msgs: Array[Any]


class Subscription:
    """What subscribe, observe and retain return: call it to stop (R-graph-surface).

    ``stop()`` is idempotent. That is the shape every runtime shares (TS gives
    ``() => void``, Rust ``FnOnce()``). For subscribe and retain it is an unsubscribe
    request (R-request-order): called during a wave, the wave's remaining arrays are
    still delivered and nothing is after it ends. For observe it takes effect at once:
    nothing more is handed on (R-graph-surface). Python adds two things: a ``with``
    block stops it on exit, and ``closed`` says whether it has stopped.
    ``Graph.close()`` stops every one of them (graphrefly-py:D16, D17).
    """

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
        """Return whether ``stop()`` has been called (a Python extension)."""

        return self._closed

    def __call__(self) -> None:
        """Stop: detach from the node, nodes or retain; calling again does nothing."""

        self._check_thread()
        if not self._closed:
            self._detach_now()
            if self._lifetime is not None:
                self._lifetime.unregister(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self()

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

        _check_dependency_scope(self._dep._ctx)
        return self._dep.id

    @property
    def waves(self) -> list[list[object]]:
        """Return this invocation's arrays from the dependency, as their DATA values."""

        _check_dependency_scope(self._dep._ctx)
        return self._dep.waves

    @property
    def terminal(self) -> ControlMessage | ErrorMessage | None:
        """Return the dependency's COMPLETE or ERROR of this invocation, or ``None``."""

        _check_dependency_scope(self._dep._ctx)
        return cast("ControlMessage | ErrorMessage | None", self._dep.terminal)

    @property
    def has(self) -> bool:
        """Return whether a value was received from the dependency and not invalidated."""

        _check_dependency_scope(self._dep._ctx)
        return self._dep.has

    @property
    def value(self) -> object:
        """Return the latest value received, or ``graphrefly.SENTINEL`` without one."""

        _check_dependency_scope(self._dep._ctx)
        dep = self._dep
        return dep.value if dep.has else SENTINEL

    @property
    def invalidated(self) -> bool:
        """Return whether an INVALIDATE arrived on this input (manual policy only)."""

        _check_dependency_scope(self._dep._ctx)
        return self._dep.invalidated

    def __repr__(self) -> str:
        return f"Dep({self.id!r})"


def _check_dependency_scope(ctx: _k.Ctx) -> None:
    try:
        ctx._check_deps()
    except _k.KernelError as error:
        raise GraphReflyRuntimeError(str(error)) from error


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
        """Return this invocation's dependency views during setup or function execution.

        Reads through this ctx or its saved views are refused after this invocation
        returns, including while a later invocation of the same node runs.
        """

        self._check()
        _check_dependency_scope(self._ctx)
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
        *,
        report: Callable[[HostReport], object] | None = None,
    ) -> None:
        self._owner_thread = get_ident()
        self._lifetime = _GraphLifetime(self._owner_thread)
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
    def producer(
        self, callback: Callable[[], T], name: str | None = None, *, recipe: Recipe | None = None
    ) -> Node[T]: ...

    @overload
    def producer(
        self, callback: None = None, name: str | None = None, *, recipe: Recipe | None = None
    ) -> Callable[[Callable[[], T]], Node[T]]: ...

    def producer(
        self,
        callback: Callable[[], T] | None = None,
        name: str | None = None,
        *,
        recipe: Recipe | None = None,
    ) -> Node[T] | Callable[[Callable[[], T]], Node[T]]:
        """Create or decorate a dependency-free node whose callback's value it sends
        once per activation (Python sugar). A ``recipe`` makes it restorable: its
        factory gives this callback again."""

        self._check_thread()
        _check_recipe(recipe)
        if callback is None:
            return lambda fn: self.producer(fn, name or fn.__name__, recipe=recipe)
        _reject_async_callable(callback)
        return self._add_node(
            name, [], _producer_run(callback), callback, None, recipe=recipe, sugar="producer"
        )

    @overload
    def derived(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[..., U],
        name: str | None = None,
        *,
        recipe: Recipe | None = None,
    ) -> Node[U]: ...

    @overload
    def derived(
        self,
        deps: Iterable[Node[Any]],
        callback: None = None,
        name: str | None = None,
        *,
        recipe: Recipe | None = None,
    ) -> Callable[[Callable[..., U]], Node[U]]: ...

    def derived(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[..., U] | None = None,
        name: str | None = None,
        *,
        recipe: Recipe | None = None,
    ) -> Node[U] | Callable[[Callable[..., U]], Node[U]]:
        """Create or decorate a value-level node over dependencies.

        Python sugar, not protocol: it runs once every dependency has a value and
        recomputes when a dependency sent DATA in the wave. A ``recipe`` makes it
        restorable: its factory gives this value-level callback again.
        """

        self._check_thread()
        _check_recipe(recipe)
        dep_list = list(deps)
        if callback is None:
            return lambda fn: self.derived(dep_list, fn, name or fn.__name__, recipe=recipe)
        _reject_async_callable(callback)
        return self._add_node(
            name, dep_list, _derived_run(callback), callback, None, recipe=recipe, sugar="derived"
        )

    @overload
    def effect(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[..., object],
        name: str | None = None,
        *,
        recipe: Recipe | None = None,
    ) -> Node[object]: ...

    @overload
    def effect(
        self,
        deps: Iterable[Node[Any]],
        callback: None = None,
        name: str | None = None,
        *,
        recipe: Recipe | None = None,
    ) -> Callable[[Callable[..., object]], Node[object]]: ...

    def effect(
        self,
        deps: Iterable[Node[Any]],
        callback: Callable[..., object] | None = None,
        name: str | None = None,
        *,
        recipe: Recipe | None = None,
    ) -> Node[object] | Callable[[Callable[..., object]], Node[object]]:
        """Create or decorate a value-level side effect over dependencies (Python
        sugar, with derived's gate); it sends nothing. A ``recipe`` makes it
        restorable: its factory gives this callback again."""

        self._check_thread()
        _check_recipe(recipe)
        dep_list = list(deps)
        if callback is None:
            return lambda fn: self.effect(dep_list, fn, name or fn.__name__, recipe=recipe)
        _reject_async_callable(callback)
        return self._add_node(
            name, dep_list, _effect_run(callback), callback, None, recipe=recipe, sugar="effect"
        )

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
        _check_recipe(recipe)
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
        _check_recipe(recipe)
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

    def find(self, node_id: str, /) -> Node[Any] | None:
        """Return the node with this id, or ``None``; it activates nothing (R-graph-surface).

        A state node comes back as its ``State``, so it can be written after a lookup or
        a restore; narrow the result with ``isinstance(node, State)``.
        """

        self._check_thread()
        if not isinstance(node_id, str):
            msg = "a node id is a str"
            raise GraphReflyValueError(msg)
        return self._nodes.get(node_id)

    def describe(self) -> list[dict[str, Any]]:
        """Return the topology as JSON-ready data (R-graph-surface, R-node).

        Every node in creation order as exactly ``{"id", "kind", "deps", "quiet"}``:
        ``kind`` is ``"state"`` or ``"node"`` and ``deps`` are the current dependencies,
        a ``ctx.rewire``'s included, while ``definition`` gives the declared ones. A
        value or a status is read from the node. It activates and runs nothing.
        """

        self._check_thread()
        return [
            {"id": d.id, "kind": d.kind, "deps": list(d.deps), "quiet": d.quiet}
            for d in self._kernel.describe()
        ]

    def observe(
        self, nodes: Iterable[Node[Any]], callback: Callable[[GraphEvent], object]
    ) -> Subscription:
        """Subscribe ``callback`` to each node as an ordinary subscriber (R-graph-surface).

        It activates a cold node and keeps it active, and gets one ``GraphEvent(id,
        msgs)`` per array a node delivers, in delivery order: the START array first,
        PENDING prefixes and the kernel's RESOLVED included. Observing the whole graph
        is observing every node ``describe`` gives. Calling the returned ``stop()`` stops
        it; nothing more is handed on after that. A callback that raises goes to the graph's
        reporter as a ``SubscriberCallbackError``, as a subscriber's does.
        """

        self._check_thread()
        _reject_async_callable(callback)
        ids = [self._node_id(node) for node in nodes]
        if len(set(ids)) != len(ids):
            msg = "observe takes each node once"
            raise GraphReflyValueError(msg)
        sink_id = self._new_sink_id()
        stopped = False

        def sink_of(node_id: str) -> Callable[[tuple[_k.Message, ...]], None]:
            def sink(array: tuple[_k.Message, ...]) -> None:
                if not stopped:
                    event = GraphEvent(node_id, cast("Array[Any]", array))
                    _observed(lambda: callback(event))

            return sink

        def stop() -> None:
            nonlocal stopped
            stopped = True  # a stop asked for during a wave runs after it
            for node_id in ids:
                self._kernel.unsubscribe(sink_id, node_id)

        def start() -> None:
            for node_id in ids:
                self._kernel.subscribe(sink_id, node_id, sink_of(node_id))

        subscription = Subscription(
            stop,
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
        )
        self._lifetime.register(subscription)
        self._call(start)
        return subscription

    # ---- host lifetime --------------------------------------------------------------

    def retain(self, node: Node[Any]) -> Subscription:
        """Hold a node active as a subscriber that ignores what it receives
        (R-graph-surface); calling the returned ``release()`` releases it. Each call is
        its own subscription, so a node retained twice stays active until both are
        released."""

        self._check_thread()
        node_id = self._node_id(node)
        sink_id = self._new_sink_id()
        subscription = Subscription(
            lambda: self._kernel.unsubscribe(sink_id, node_id),
            owner_thread=self._owner_thread,
            lifetime=self._lifetime,
        )
        self._lifetime.register(subscription)
        self._call(lambda: self._kernel.subscribe(sink_id, node_id))
        return subscription

    def reentry_queue(self) -> GraphReentryQueue:
        """Return this graph's explicit owner-thread async re-entry queue."""

        from graphrefly._async import _GRAPH_REENTRY_QUEUE_TOKEN, GraphReentryQueue

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
            _reject_awaitable(callback(Ctx(ctx, self)))

        return run

    def _ctx_setup(self, setup: SetupCallback | None) -> Callable[[_k.Ctx], object] | None:
        if setup is None:
            return None
        start = setup

        def run(ctx: _k.Ctx) -> object:
            cleanup = start(Ctx(ctx, self))
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
        sugar: str | None = None,
    ) -> Node[Any]:
        ids = [self._node_id(dep) for dep in deps]
        node_id = self._new_id(name)
        kernel_recipe = _kernel_recipe(recipe, sugar)
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
        if isinstance(name, str) and "::" in name:
            raise GraphReflyValueError(NAME_ERROR)
        if isinstance(name, str) and (name.startswith(":") or name.endswith(":")):
            raise GraphReflyValueError(BOUNDARY_NAME_ERROR)
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
        if not allow_closed and self._lifetime.is_closed():
            raise GraphReflyRuntimeError(self._lifetime.closed_message)


def _check_recipe(recipe: object) -> None:
    if recipe is not None and not isinstance(recipe, Recipe):
        msg = "recipe must be a graphrefly.Recipe"
        raise GraphReflyValueError(msg)


def _kernel_recipe(recipe: Recipe | None, sugar: str | None) -> dict[str, object] | None:
    """The opaque recipe the kernel keeps: the ref, the config and, for sugar, which
    sugar rebuilds the node's function on restore."""

    if recipe is None:
        return None
    kept: dict[str, object] = {"ref": recipe.ref, "config": recipe.config}
    if sugar is not None:
        kept["sugar"] = sugar
    return kept


def _producer_run(produce: Callable[[], object]) -> Callable[[_k.Ctx], None]:
    def run(ctx: _k.Ctx) -> None:
        value = produce()
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        ctx.down([(_k.DATA, value)])

    return run


def _derived_run(compute: Callable[..., object]) -> Callable[[_k.Ctx], None]:
    def run(ctx: _k.Ctx) -> None:
        values = _gate(ctx)
        if values is None:
            return
        value = compute(*values)
        _reject_awaitable(value)
        _reject_sentinel_data(value)
        ctx.down([(_k.DATA, value)])

    return run


def _effect_run(act: Callable[..., object]) -> Callable[[_k.Ctx], None]:
    def run(ctx: _k.Ctx) -> None:
        values = _gate(ctx)
        if values is None:
            return
        _reject_awaitable(act(*values))

    return run


# What restore rebuilds a sugar node's function with, from its factory's callback.
_SUGAR: Final[dict[str, Callable[[Callable[..., Any]], Callable[[_k.Ctx], None]]]] = {
    "producer": _producer_run,
    "derived": _derived_run,
    "effect": _effect_run,
}


def _gate(ctx: _k.Ctx) -> list[object] | None:
    """derived's sugar: every dependency has a value and one sent DATA in this wave."""

    deps = ctx.deps
    fresh = False
    for dep in deps:
        if not dep.has:
            return None
        fresh = fresh or dep.sent_data
    if not fresh:
        return None
    return [dep.value for dep in deps]


def version() -> str:
    """Return the installed Python package version."""

    return _VERSION


def restore_graph(
    checkpoint: GraphCheckpoint,
    *,
    registry: Mapping[str, RecipeFactory],
    report: Callable[[HostReport], object] | None = None,
) -> Graph:
    """Build a new graph from ``checkpoint`` (R-checkpoint); no callback runs.

    ``registry`` maps each recipe's ``ref`` to a factory called as
    ``factory(config, RecipeContext(graph, name, make))``, returning the node callback or
    a ``(callback, setup)`` pair; ``make`` builds another registered recipe. A node
    built with ``derived``, ``effect`` or ``producer`` and a recipe gets its value-level
    callback from the factory, with no setup, and the same sugar again. State
    nodes need no recipe. A node built without a recipe is local-only, and restoring it
    fails, as do a missing recipe and a checkpoint in another format.
    """

    if not isinstance(registry, Mapping):
        msg = "registry must map recipe refs to factories"
        raise GraphReflyValueError(msg)
    try:
        admitted = _checkpoint._admit(checkpoint)
    except _checkpoint.CheckpointError as error:
        raise GraphReflyRestoreError(str(error)) from error
    graph = Graph(report=report)

    def build(
        ref: str, config: object, node_name: str
    ) -> tuple[NodeCallback, SetupCallback | None]:
        factory = registry.get(ref)
        if factory is None:
            raise GraphReflyRestoreError(f"not restorable: {node_name}: missing recipe")
        made = factory(config, RecipeContext(graph, node_name, build))
        if isinstance(made, tuple) and len(made) != 2:
            msg = f"recipe {ref} must give a callback, or a (callback, setup) pair"
            raise GraphReflyRestoreError(msg)
        callback, setup = made if isinstance(made, tuple) else (made, None)
        if not callable(callback) or (setup is not None and not callable(setup)):
            msg = f"recipe {ref} must give a callback, or a (callback, setup) pair"
            raise GraphReflyRestoreError(msg)
        return callback, setup

    sugar_of = _sugar_by_node({"nodes": admitted.nodes})

    def adapt(ref: str) -> _checkpoint.Factory:
        def make(config: object, at: _checkpoint.RecipeContext) -> _checkpoint.Built:
            callback, setup = build(ref, config, at.id)
            sugar = sugar_of.get(at.id)
            if sugar is not None:  # a derived, effect or producer: the same gate again
                if setup is not None:
                    msg = f"recipe {ref} for the {sugar} {at.id} gives a setup; sugar has none"
                    raise GraphReflyRestoreError(msg)
                _reject_async_callable(callback)
                return _checkpoint.Built(_guard_fn(_SUGAR[sugar](callback), callback))
            return _checkpoint.Built(
                _guard_fn(graph._ctx_callback(callback), callback),
                _guard_setup(graph._ctx_setup(setup), setup),
            )

        return make

    adapted = {ref: adapt(ref) for ref in registry}
    try:
        _checkpoint._restore_admitted(admitted, adapted, into=graph._kernel)
    except _checkpoint.CheckpointError as error:
        raise GraphReflyRestoreError(str(error)) from error
    for described in graph._kernel.describe():
        handle: Node[Any] = (
            State(graph, described.id) if described.kind == "state" else Node(graph, described.id)
        )
        graph._register(handle)
    return graph


def _sugar_by_node(checkpoint: object) -> dict[str, str]:
    """Which saved nodes were built with sugar, read from their recipes; the checkpoint
    module checks the rest of the checkpoint."""

    nodes = checkpoint.get("nodes") if isinstance(checkpoint, dict) else None
    out: dict[str, str] = {}
    for node in nodes if isinstance(nodes, list) else []:
        recipe = node.get("recipe") if isinstance(node, dict) else None
        sugar = recipe.get("sugar") if isinstance(recipe, dict) else None
        if isinstance(sugar, str) and sugar in _SUGAR and isinstance(node.get("id"), str):
            out[node["id"]] = sugar
    return out


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


# Exact types that are never awaitable; a subclass still goes through isawaitable.
_NOT_AWAITABLE: Final = frozenset(
    {type(None), bool, int, float, complex, str, bytes, tuple, list, dict, set, frozenset}
)


def _reject_awaitable(value: object) -> None:
    if type(value) in _NOT_AWAITABLE:
        return
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


def _guard[V](
    fn: Callable[[_k.Ctx], V], authored: Callable[..., object] | None
) -> Callable[[_k.Ctx], V]:
    """The one place a user callback's Exception becomes a GraphCallbackError, which the
    kernel turns into the node's ERROR; a fatal error or a BaseException passes through.

    Every node function and setup the facade gives the kernel goes through here. The
    result carries the user's callback, which ``Graph.definition`` reads from the kernel.
    """

    def run(ctx: _k.Ctx) -> V:
        try:
            return fn(ctx)
        except (GraphCallbackError, *_k.FATAL):
            raise
        except Exception as error:
            raise _callback_error(error) from error

    setattr(run, _AUTHORED, authored)
    return run


def _guard_fn(
    fn: Callable[[_k.Ctx], None], authored: Callable[..., object] | None = None
) -> Callable[[_k.Ctx], None]:
    return _guard(fn, authored)


def _guard_setup(
    setup: Callable[[_k.Ctx], object] | None, authored: SetupCallback | None = None
) -> Callable[[_k.Ctx], object] | None:
    return None if setup is None else _guard(setup, authored)


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
