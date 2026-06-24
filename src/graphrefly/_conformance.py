"""Private fixed-stimulus harness for GraphReFly conformance tests.

D447 permits this module for approved scenario stimuli over private native
internals. It is deliberately not exported from ``graphrefly.__all__``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from threading import get_ident
from typing import Any, Literal

from graphrefly import _native
from graphrefly._facade import (
    Ctx,
    Graph,
    Node,
    _poison_on_fatal,
    _reject_async_callable,
    _reject_awaitable,
    _reject_sentinel_data,
)
from graphrefly.exceptions import GraphReflyRuntimeError, GraphReflyValueError

PausableMode = Literal["true", "resumeAll", "false"]


class ConformanceAsyncHandle:
    """Private scenario-fixed handle for a captured async-pool ctx."""

    def __init__(self, native: Any, *, owner_thread: int, lifetime: Any) -> None:
        self._native = native
        self._owner_thread = owner_thread
        self._lifetime = lifetime

    def has_pending(self) -> bool:
        self._check_thread()
        try:
            return bool(self._native.has_pending())
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def resolve(self, value: object) -> None:
        self._check_thread()
        _reject_sentinel_data(value)
        _reject_awaitable(value)
        try:
            self._native.resolve(value)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def invalidate_live_deps(self) -> None:
        self._check_thread()
        try:
            self._native.invalidate_live_deps()
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._lifetime, error)

    def _check_thread(self) -> None:
        if get_ident() != self._owner_thread:
            msg = (
                "GraphReFly Python conformance async handles are bound to "
                "their creating thread in v0"
            )
            raise GraphReflyRuntimeError(msg)
        if self._lifetime.closed:
            raise GraphReflyRuntimeError(self._lifetime.closed_message)


class ConformanceStimulus:
    """Scenario-named private stimuli; not a generic message sender."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph

    def state_empty(self, name: str | None = None) -> Node[object]:
        self._graph._check_thread()
        try:
            return Node(
                self._graph._native.state_empty(name),
                owner_thread=self._graph._owner_thread,
                lifetime=self._graph._lifetime,
                writable=True,
            )
        except ValueError as error:
            raise GraphReflyValueError(str(error)) from error
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._graph._lifetime, error)

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
        pausable: PausableMode = "true",
        pull_id: str | None = None,
    ) -> Node[object]:
        self._graph._check_thread()
        deps = list(deps)
        native_deps = self._graph._native_deps(deps)

        def native_callback(native_ctx: _native.Ctx) -> None:
            value = callback(
                Ctx(
                    native_ctx,
                    owner_thread=self._graph._owner_thread,
                    lifetime=self._graph._lifetime,
                )
            )
            _reject_awaitable(value)

        try:
            return Node(
                self._graph._native._conformance_node(
                    native_deps,
                    native_callback,
                    name,
                    partial,
                    complete_when_deps_complete,
                    error_when_deps_error,
                    terminal_as_real_input,
                    pausable,
                    pull_id,
                ),
                owner_thread=self._graph._owner_thread,
                lifetime=self._graph._lifetime,
            )
        except ValueError as error:
            raise GraphReflyValueError(str(error)) from error
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._graph._lifetime, error)

    def c7_send_unheld_dirty_up(self, node: Node[Any]) -> None:
        self._native_node(node)._up_dirty()

    def c7_send_unheld_teardown_up(self, node: Node[Any]) -> None:
        self._native_node(node)._up_teardown()

    def c15_dep_goes_dirty(self, node: Node[Any]) -> None:
        self._native_node(node)._down_dirty()

    def c15_dirty_dep_completes_without_data(self, node: Node[Any]) -> None:
        self._native_node(node)._down_complete()

    def c15_dirty_dep_errors_without_data(self, node: Node[Any], diagnostic: str) -> None:
        self._native_node(node)._down_error(diagnostic)

    def c17_dep_errors(self, node: Node[Any], diagnostic: str) -> None:
        self._native_node(node)._down_error(diagnostic)

    def c17_dep_emits_data_then_completes(self, node: Node[Any], value: object) -> None:
        _reject_sentinel_data(value)
        self._native_node(node)._down_data_complete(value)

    def c19_dep_invalidates_after_dirty(self, node: Node[Any]) -> None:
        self._native_node(node)._down_invalidate()

    def c20_dep_tears_down(self, node: Node[Any]) -> None:
        self._native_node(node)._down_teardown()

    def c23_dep_dirty_then_resolved(self, node: Node[Any]) -> None:
        native = self._native_node(node)
        native._up_dirty()
        native._down_resolved()

    def c23_dep_emits_data_data_invalidate(
        self,
        node: Node[Any],
        first: object,
        second: object,
    ) -> None:
        _reject_sentinel_data(first)
        _reject_sentinel_data(second)
        self._native_node(node)._down_data_data_invalidate(first, second)

    def c12_dep_emits_data_resolved(self, node: Node[Any], value: object) -> None:
        _reject_sentinel_data(value)
        _reject_awaitable(value)
        self._native_node(node)._conformance_c12_down_data_resolved(value)

    def c23_dep_completes(self, node: Node[Any]) -> None:
        self._native_node(node)._down_complete()

    def c23_dep_errors(self, node: Node[Any], diagnostic: str) -> None:
        self._native_node(node)._down_error(diagnostic)

    def c16_pull(self, node: Node[Any], pull_id: str, params: object | None = None) -> None:
        _reject_sentinel_data(params)
        self._native_node(node)._conformance_up_pull(pull_id, params)

    def c16_pull_toward(
        self,
        node: Node[Any],
        toward_dep: int,
        pull_id: str,
        params: object | None = None,
    ) -> None:
        _reject_sentinel_data(params)
        self._native_node(node)._conformance_up_pull_toward(toward_dep, pull_id, params)

    def c26_send_forbidden_data_up(self, node: Node[Any], value: object) -> None:
        _reject_sentinel_data(value)
        self._native_node(node)._conformance_up_data_forbidden(value)

    def c11_immediate_subscribe_dep(
        self,
        node: Node[Any],
        dep: Node[Any],
        callback: Callable[[Ctx], object],
    ) -> None:
        def native_callback(native_ctx: _native.Ctx) -> None:
            value = callback(
                Ctx(
                    native_ctx,
                    owner_thread=self._graph._owner_thread,
                    lifetime=self._graph._lifetime,
                )
            )
            _reject_awaitable(value)

        self._native_node(node)._conformance_immediate_subscribe_dep(
            self._native_node(dep),
            native_callback,
        )

    def c8_immediate_subscribe_dep(
        self,
        node: Node[Any],
        dep: Node[Any],
        callback: Callable[[Ctx], object],
    ) -> None:
        native_callback = self._native_callback(callback)
        try:
            self._native_node(node)._conformance_immediate_subscribe_dep(
                self._native_node(dep),
                native_callback,
            )
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._graph._lifetime, error)

    def c8_immediate_unsubscribe_dep(
        self,
        node: Node[Any],
        dep: Node[Any],
        callback: Callable[[Ctx], object],
    ) -> None:
        native_callback = self._native_callback(callback)
        try:
            self._native_node(node)._conformance_immediate_unsubscribe_dep(
                self._native_node(dep),
                native_callback,
            )
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._graph._lifetime, error)

    def c8_immediate_replace_deps(
        self,
        node: Node[Any],
        deps: Iterable[Node[Any]],
        callback: Callable[[Ctx], object],
    ) -> None:
        native_callback = self._native_callback(callback)
        try:
            self._native_node(node)._conformance_immediate_replace_deps(
                [self._native_node(dep) for dep in deps],
                native_callback,
            )
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._graph._lifetime, error)

    def c22_old_shape_data(self, node: Node[Any], value: object) -> None:
        _reject_sentinel_data(value)
        _reject_awaitable(value)
        try:
            self._native_node(node)._conformance_c22_down_data(value)
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._graph._lifetime, error)

    def c2_async_result_node(
        self,
        dep: Node[Any],
        name: str | None = None,
    ) -> tuple[Node[object], ConformanceAsyncHandle]:
        return self._async_node([dep], name=name, pausable="true")

    def c4_async_diamond_leg(
        self,
        dep: Node[Any],
        name: str | None = None,
    ) -> tuple[Node[object], ConformanceAsyncHandle]:
        return self._async_node([dep], name=name, pausable="true")

    def c9_pausable_false_async_source(
        self,
        name: str | None = None,
    ) -> tuple[Node[object], ConformanceAsyncHandle]:
        return self._async_source(name=name, pausable="false")

    def c10_true_mode_async_leaf_source(
        self,
        name: str | None = None,
    ) -> tuple[Node[object], ConformanceAsyncHandle]:
        return self._async_source(name=name, pausable="true")

    def c21_live_edge_async_node(
        self,
        dep: Node[Any],
        name: str | None = None,
    ) -> tuple[Node[object], ConformanceAsyncHandle]:
        return self._async_node([dep], name=name, pausable="true")

    def c21_replace_with_live_dep(
        self,
        node: Node[Any],
        dep: Node[Any],
        pending: ConformanceAsyncHandle,
    ) -> None:
        if pending._lifetime is not self._graph._lifetime:
            msg = "conformance async handle must belong to this GraphReFly graph"
            raise GraphReflyRuntimeError(msg)
        self._native_node(node)._conformance_c21_replace_with_live_dep(
            self._native_node(dep),
            pending._native,
        )

    def _native_node(self, node: Node[Any]) -> _native.Node:
        if not isinstance(node, Node):
            msg = "conformance stimulus nodes must be graphrefly.Node objects"
            raise GraphReflyValueError(msg)
        return self._graph._native_node(node)

    def _native_callback(self, callback: Callable[[Ctx], object]) -> Callable[[_native.Ctx], None]:
        if not callable(callback):
            msg = "conformance rewire callback must be callable"
            raise GraphReflyValueError(msg)
        _reject_async_callable(callback)

        def native_callback(native_ctx: _native.Ctx) -> None:
            value = callback(
                Ctx(
                    native_ctx,
                    owner_thread=self._graph._owner_thread,
                    lifetime=self._graph._lifetime,
                )
            )
            _reject_awaitable(value)

        return native_callback

    def _async_node(
        self,
        deps: Iterable[Node[Any]],
        *,
        name: str | None,
        pausable: PausableMode,
    ) -> tuple[Node[object], ConformanceAsyncHandle]:
        self._graph._check_thread()
        native_deps = self._graph._native_deps(list(deps))
        try:
            native_node, native_handle = self._graph._native._conformance_async_node(
                native_deps,
                name,
                pausable,
            )
            return (
                Node(
                    native_node,
                    owner_thread=self._graph._owner_thread,
                    lifetime=self._graph._lifetime,
                ),
                ConformanceAsyncHandle(
                    native_handle,
                    owner_thread=self._graph._owner_thread,
                    lifetime=self._graph._lifetime,
                ),
            )
        except ValueError as error:
            raise GraphReflyValueError(str(error)) from error
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._graph._lifetime, error)

    def _async_source(
        self,
        *,
        name: str | None,
        pausable: PausableMode,
    ) -> tuple[Node[object], ConformanceAsyncHandle]:
        self._graph._check_thread()
        try:
            native_node, native_handle = self._graph._native._conformance_async_source(
                name,
                pausable,
            )
            return (
                Node(
                    native_node,
                    owner_thread=self._graph._owner_thread,
                    lifetime=self._graph._lifetime,
                ),
                ConformanceAsyncHandle(
                    native_handle,
                    owner_thread=self._graph._owner_thread,
                    lifetime=self._graph._lifetime,
                ),
            )
        except ValueError as error:
            raise GraphReflyValueError(str(error)) from error
        except RuntimeError as error:
            raise GraphReflyRuntimeError(str(error)) from error
        except BaseException as error:
            _poison_on_fatal(self._graph._lifetime, error)


def up_data_forbidden(ctx: Ctx, value: object) -> None:
    _reject_sentinel_data(value)
    ctx._native._conformance_up_data(value)


def down_complete(ctx: Ctx) -> None:
    ctx._native._conformance_down_complete()
