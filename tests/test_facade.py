"""The public Python facade on the Python kernel (graphrefly-py:D2, D4, D5).

The kernel scenarios are qualified in tests/kernel; these tests cover the facade:
its API shape, value boundary, exception taxonomy, host lifetime and checkpoint.
"""

from __future__ import annotations

import functools
import gc
import logging
import operator
import threading
from collections.abc import Callable
from typing import Any

import pytest

import graphrefly
from graphrefly import (
    COMPLETE,
    DATA,
    SENTINEL,
    CallbackError,
    Ctx,
    Graph,
    GraphCallbackError,
    GraphEvent,
    GraphReflyCheckpointError,
    GraphReflyNoDataError,
    GraphReflyRestoreError,
    GraphReflyRuntimeError,
    GraphReflyValueError,
    HostReport,
    Message,
    Node,
    NodeStatus,
    Recipe,
    RecipeContext,
    Signal,
    State,
    SubscriberCallbackError,
    Subscription,
)

Arrays = list[tuple[Message[Any], ...]]


def kinds(arrays: Arrays) -> list[list[str]]:
    return [[m[0] for m in array] for array in arrays]


def data(arrays: Arrays) -> list[object]:
    return [m[1] for array in arrays for m in array if m[0] == "DATA"]


# ---- package surface ------------------------------------------------------------


def test_package_surface_is_the_d4_api() -> None:
    # The whole surface, exactly: a removed name fails here without a separate list.
    expected = {
        # graph and nodes
        "Graph",
        "Node",
        "State",
        "Ctx",
        "Dep",
        "Subscription",
        "Signal",
        "NodeStatus",
        "NodeDefinition",
        "GraphEvent",
        "HostReport",
        "SENTINEL",
        "Sentinel",
        # messages
        "START",
        "PENDING",
        "DATA",
        "RESOLVED",
        "INVALIDATE",
        "COMPLETE",
        "ERROR",
        "Message",
        "DataMessage",
        "ErrorMessage",
        "ControlMessage",
        # checkpoint and restore
        "GraphCheckpoint",
        "Recipe",
        "RecipeContext",
        "RecipeFactory",
        "restore_graph",
        # async and network
        "AsyncRunner",
        "from_awaitable",
        "from_async_iter",
        "async_node",
        "asyncio_runner",
        "trio_runner",
        "anyio_runner",
        "GraphReentryQueue",
        "from_http",
        "from_sse",
        "HttpRequest",
        "HttpResponse",
        "HttpStreamHead",
        "HttpStreamDriverEvent",
        "HttpStreamHeadEvent",
        "HttpStreamChunkEvent",
        "HttpStreamCompleteEvent",
        "HttpStreamErrorEvent",
        "LocalHttpDriver",
        "LocalHttpStreamDriver",
        "SseEvent",
        # wire, in the D803 shape (graphrefly-py:D8)
        "wire_edge_group_out",
        "wire_edge_group_in",
        "frame_source",
        "frame_sink",
        "wire_bridge",
        "wire_bridge_ack_driver",
        "WireEdgeArrays",
        "WireEdgeCause",
        "WireEdgeGroupIn",
        "WireEdgeGroupIssue",
        "WireBridge",
        "WireBridgeIssue",
        "WireBridgeStatus",
        "WireBridgeAckTimeout",
        "WireBridgeAckDriver",
        "WireBridgeAckDriverIssue",
        "WireBridgeAckDriverStatus",
        # exceptions
        "GraphReflyError",
        "GraphReflyRuntimeError",
        "GraphReflyValueError",
        "GraphReflyCheckpointError",
        "GraphReflyRestoreError",
        "GraphReflyNoDataError",
        "CallbackError",
        "GraphCallbackError",
        "SubscriberCallbackError",
        # version
        "version",
        "__version__",
    }
    assert set(graphrefly.__all__) == expected


def test_the_package_imports_no_native_module() -> None:
    import sys

    assert not hasattr(graphrefly, "_native")
    assert "graphrefly._native" not in sys.modules


def test_host_input_lives_only_on_state() -> None:
    for name in ("set", "invalidate", "complete", "error", "down"):
        assert not hasattr(Node, name)
    for name in ("set", "invalidate", "complete", "error"):
        assert hasattr(State, name)
    for name in ("invalidate", "pause", "resume", "set", "down"):
        assert not hasattr(Graph, name)
    for name in ("on_invalidate", "on_deactivation", "request_pull", "rewire_next", "up"):
        assert not hasattr(Ctx, name)


def test_no_equality_dedup_surface() -> None:
    from inspect import signature

    assert "equals" not in signature(Graph.node).parameters
    assert "equals" not in signature(Graph.derived).parameters


# ---- values and arrays ------------------------------------------------------------


def test_subscribe_gets_one_tuple_per_array_starting_with_start() -> None:
    with Graph("arrays") as g:
        a = g.state(1, name="a")
        d = g.derived([a], lambda v: v * 2, name="d")
        seen: Arrays = []
        d.subscribe(seen.append)
        a.set(3)
        assert seen == [
            (("START",), ("DATA", 2)),
            (("PENDING",), ("DATA", 6)),
        ]


def test_none_is_valid_data_and_absence_is_not_none() -> None:
    with Graph("none") as g:
        a = g.state(None, name="a")
        assert a.cache() is None and a.has_value
        empty = g.state(name="empty")
        assert not empty.has_value
        with pytest.raises(GraphReflyNoDataError):
            empty.cache()
        assert empty.cache("fallback") == "fallback"
        with pytest.raises(GraphReflyValueError, match="SENTINEL"):
            a.set(SENTINEL)
        with pytest.raises(GraphReflyValueError, match="SENTINEL"):
            g.state(SENTINEL)


def test_set_with_a_callable_computes_from_the_current_value() -> None:
    with Graph("set-fn") as g:
        a = g.state(1, name="a")
        seen: Arrays = []
        a.subscribe(seen.append)
        a.set(lambda v: v + 1)
        a.set(lambda v: v)  # returns its argument: nothing is written
        empty = g.state(name="empty")
        got: list[object] = []
        empty.set(lambda v: got.append(v) or 5)
        assert got == [SENTINEL] and empty.cache() == 5
        a.set(lambda _: print)  # a callable value is written wrapped
        assert a.cache() is print
        assert data(seen) == [1, 2, print]


def test_invalidate_complete_and_error_are_state_host_inputs() -> None:
    with Graph("host") as g:
        a = g.state(1, name="a")
        seen: Arrays = []
        a.subscribe(seen.append)
        a.invalidate()
        assert not a.has_value
        a.set(2)
        a.complete()
        assert a.status == NodeStatus("ready", "completed", "active")
        with pytest.raises(GraphReflyRuntimeError, match="has ended"):
            a.set(3)
        b = g.state(0, name="b")
        b.error({"code": 7})
        assert b.status.output == "errored"
        with pytest.raises(GraphReflyValueError):
            g.state(0).error(True)
        assert kinds(seen)[1:] == [
            ["PENDING", "INVALIDATE"],
            ["PENDING", "DATA"],
            ["PENDING", "COMPLETE"],
        ]


def test_batch_returns_its_result_commits_once_and_a_raising_body_commits_nothing() -> None:
    with Graph("batch") as g:
        a = g.state(0, name="a")
        seen: Arrays = []
        a.subscribe(seen.append)

        def body() -> str:
            a.set(1)
            a.set(2)
            return "done"

        assert g.batch(body) == "done"
        assert seen[-1] == (("PENDING",), ("DATA", 1), ("DATA", 2))

        class Boom(Exception):
            pass

        def failing() -> None:
            a.set(3)
            raise Boom

        with pytest.raises(Boom):
            g.batch(failing)
        assert a.cache() == 2

        async def async_body() -> None:
            return None

        with pytest.raises(GraphReflyRuntimeError, match="async"):
            g.batch(async_body)  # type: ignore[arg-type]


# ---- advanced nodes and Ctx ----------------------------------------------------------


def test_node_ctx_reads_inputs_and_emits_one_array_per_call() -> None:
    with Graph("ctx") as g:
        a = g.state(None, name="a")
        b = g.state(name="b")
        reads: list[tuple[object, ...]] = []

        @g.node([a, b])
        def n(ctx: Ctx) -> None:
            first, second = ctx.deps
            reads.append(
                (
                    len(ctx.deps),
                    (first.id, first.has, first.value),
                    (second.id, second.has, second.value),
                    [dep.waves for dep in ctx.deps],
                )
            )
            ctx.emit(None, 1)
            ctx.emit(2)

        seen: Arrays = []
        n.subscribe(seen.append)
        assert n.name == "n"
        # None is DATA; a dependency without a value reads as SENTINEL (graphrefly-py:D12).
        assert reads == [(2, ("a", True, None), ("b", False, SENTINEL), [[[None]], []])]
        assert seen == [(("START",), ("DATA", 2))]  # the last array with DATA
        b.set("x")
        assert seen[-2:] == [
            (("PENDING",), ("DATA", None), ("DATA", 1)),
            (("PENDING",), ("DATA", 2)),
        ]


def test_ctx_deps_terminal_and_invalidated_reads() -> None:
    with Graph("ctx-reads") as g:
        a = g.state(1, name="a")
        seen: list[object] = []

        def n(ctx: Ctx) -> None:
            (dep,) = ctx.deps
            seen.append((dep.terminal, dep.invalidated))
            with pytest.raises(IndexError):
                ctx.deps[5]  # noqa: B018
            assert not hasattr(ctx, "data") and not hasattr(ctx, "terminal")

        node = g.node([a], n, complete="manual", invalidate="manual")
        node.subscribe(lambda _: None)
        a.invalidate()
        a.complete()
        assert seen == [(None, False), (None, True), (("COMPLETE",), False)]


def test_ctx_down_sends_mixed_arrays_and_rejects_kernel_only_types() -> None:
    with Graph("down") as g:
        a = g.state(1, name="a")
        rejected: list[str] = []

        def n(ctx: Ctx) -> None:
            for bad in ([("START",)], [("PENDING",)], [("RESOLVED",)], ("DATA", 1)):
                try:
                    ctx.down(bad)  # type: ignore[arg-type]
                except GraphReflyValueError as error:
                    rejected.append(str(error))
            ctx.down([(DATA, ctx.deps[0].value), (COMPLETE,)])

        node = g.node([a], n)
        seen: Arrays = []
        node.subscribe(seen.append)
        assert len(rejected) == 4  # three kernel-only types, and a message that is no list
        assert seen == [(("START",), ("DATA", 1), ("COMPLETE",))]


def test_a_callback_exception_is_the_nodes_error_as_graph_callback_error() -> None:
    with Graph("error") as g:
        a = g.state(1, name="a")
        boom = ValueError("boom")

        def n(ctx: Ctx) -> None:
            raise boom

        node = g.node([a], n)
        seen: Arrays = []
        node.subscribe(seen.append)
        (start, error) = seen[0]
        assert start == ("START",)
        assert error[0] == "ERROR" and isinstance(error[1], GraphCallbackError)
        assert (error[1].type_name, error[1].message, error[1].raw) == (
            "ValueError",
            "boom",
            boom,
        )
        assert node.status.output == "errored"


def test_an_uncaught_rejected_down_is_the_nodes_error() -> None:
    with Graph("reject") as g:
        a = g.state(1, name="a")
        node = g.node([a], lambda ctx: ctx.down([("START",)]))
        seen: Arrays = []
        node.subscribe(seen.append)
        error = seen[0][1]
        assert error[0] == "ERROR" and isinstance(error[1], GraphCallbackError)
        assert error[1].type_name == "GraphReflyValueError"


def test_async_callbacks_are_rejected_at_registration() -> None:
    async def callback(*_: object) -> None:
        return None

    with Graph("async") as g:
        a = g.state(1)
        with pytest.raises(GraphReflyRuntimeError, match="async"):
            g.derived([a], callback)
        with pytest.raises(GraphReflyRuntimeError, match="async"):
            g.node([a], callback)
        with pytest.raises(GraphReflyRuntimeError, match="async"):
            a.subscribe(callback)


def test_an_awaitable_returned_by_a_callback_is_an_error_without_leaking_it() -> None:
    async def later() -> None:
        return None

    with Graph("awaitable") as g:
        a = g.state(1)
        node = g.node([a], lambda ctx: later())
        seen: Arrays = []
        node.subscribe(seen.append)
        error = seen[0][1]
        assert error[0] == "ERROR" and isinstance(error[1], GraphCallbackError)
        assert error[1].type_name == "CallbackError"


def test_setup_runs_once_per_lifetime_and_its_cleanup_at_close() -> None:
    log: list[str] = []
    with Graph("setup") as g:
        a = g.state(1, name="a")

        def setup(ctx: Ctx) -> Any:
            log.append("setup")
            assert isinstance(ctx.signal, Signal)
            ctx.signal.on_abort(lambda: log.append("abort"))
            return lambda: log.append(f"cleanup state={ctx.state}")

        def n(ctx: Ctx) -> None:
            ctx.state = ctx.deps[0].value

        node = g.node([a], n, setup=setup)
        sub = node.subscribe(lambda _: None)
        a.set(2)
        sub.unsubscribe()
        assert log == ["setup", "abort", "cleanup state=2"]


def test_a_cleanup_failure_goes_to_the_host_reporter_not_the_node() -> None:
    reports: list[HostReport] = []
    with Graph("cleanup", report=reports.append) as g:
        a = g.state(1)

        def setup(ctx: Ctx) -> Any:
            def cleanup() -> None:
                raise RuntimeError("cleanup failed")

            return cleanup

        node = g.node([a], lambda ctx: None, setup=setup)
        node.subscribe(lambda _: None).unsubscribe()
        assert [(r.source, str(r.error)) for r in reports] == [
            (f"cleanup {node.name}", "cleanup failed")
        ]
        assert node.status.output == "open"


def test_policies_quiet_and_manual_invalidate() -> None:
    with Graph("policies") as g:
        a = g.state(1, name="a")
        runs: list[list[bool]] = []

        def n(ctx: Ctx) -> None:
            runs.append([ctx.deps[0].invalidated])

        node = g.node([a], n, invalidate="manual")
        node.subscribe(lambda _: None)
        a.invalidate()
        assert runs == [[False], [True]]
        with pytest.raises(GraphReflyValueError):
            g.node([a], n, complete="sometimes")  # type: ignore[arg-type]
        quiet = g.node([a], lambda ctx: None, quiet=True)
        assert g.definition(quiet).quiet is True


def test_ctx_rewire_replace_and_definition() -> None:
    with Graph("topology") as g:
        a = g.state(1, name="a")
        b = g.state(10, name="b")
        kept: list[Ctx] = []

        def total(ctx: Ctx) -> None:
            kept.append(ctx)
            ctx.emit(sum(d.value if d.has else 0 for d in ctx.deps))

        n = g.node([a], total, name="n", recipe=Recipe("total"))
        n.subscribe(lambda _: None)
        kept[-1].rewire([a, b])
        assert n.cache() == 11
        definition = g.definition(n)
        assert definition.deps == (a,) and definition.callback is total
        assert definition.recipe == Recipe("total")
        assert [entry["deps"] for entry in g.describe() if entry["id"] == "n"] == [["a", "b"]]
        g.replace(n, [b], lambda ctx: ctx.emit(ctx.deps[0].value * 2))
        assert n.cache() == 20
        assert g.definition(n).recipe is None
        with pytest.raises(GraphReflyRuntimeError, match="not a computed node"):
            g.replace(a, [], lambda ctx: None)


# ---- describe and observe ------------------------------------------------------------


def test_describe_is_exactly_the_topology_of_each_node() -> None:
    with Graph("describe") as g:
        a = g.state(1, name="a")
        d = g.derived([a], lambda v: v, name="d")
        assert g.describe() == [
            {"id": "a", "kind": "state", "deps": [], "quiet": False},
            {"id": "d", "kind": "node", "deps": ["a"], "quiet": False},
        ]
        assert d.status.lifecycle == "inactive"  # describe activates nothing


def test_observe_is_a_subscriber_with_one_event_per_array() -> None:
    with Graph("observe") as g:
        a = g.state(1, name="a")
        d = g.derived([a], lambda v: v + 1, name="d")
        events: list[GraphEvent] = []
        sub = g.observe([a, d], events.append)
        assert d.status.lifecycle == "active"  # observing a cold node activates it
        assert [(e.id, e.msgs) for e in events] == [
            ("a", (("START",), ("DATA", 1))),
            ("d", (("START",), ("DATA", 2))),
        ]
        events.clear()
        a.set(2)
        assert [(e.id, e.msgs) for e in events] == [
            ("a", (("PENDING",), ("DATA", 2))),
            ("d", (("PENDING",), ("DATA", 3))),
        ]
        assert not hasattr(events[0], "seq")
        sub.unsubscribe()
        assert d.status.lifecycle == "inactive"
        events.clear()
        a.set(3)
        assert events == []
        with pytest.raises(GraphReflyValueError, match="each node once"):
            g.observe([a, a], events.append)


def test_an_observer_stopped_in_its_callback_gets_nothing_more() -> None:
    with Graph("observe-stop") as g:
        a = g.state(1, name="a")
        b = g.derived([a], lambda v: v * 10, name="b")
        events: list[GraphEvent] = []

        def once(event: GraphEvent) -> None:
            events.append(event)
            if event.msgs[0] == ("PENDING",):
                sub.unsubscribe()  # during the wave: b's array must not follow

        sub = g.observe([a, b], once)
        a.set(2)
        assert [(e.id, e.msgs) for e in events][2:] == [("a", (("PENDING",), ("DATA", 2)))]


def test_an_observer_failure_goes_to_the_reporter() -> None:
    reports: list[HostReport] = []
    with Graph("observe-errors", report=reports.append) as g:
        a = g.state(1)

        def failing(event: GraphEvent) -> None:
            if event.msgs[0] != ("START",):
                raise RuntimeError("observer failed")

        g.observe([a], failing)
        a.set(2)
        assert [(type(r.error), str(r.error)) for r in reports] == [
            (SubscriberCallbackError, "observer failed")
        ]
        assert reports[0].source.startswith("sink ")


# ---- subscriber boundary -----------------------------------------------------------------


def test_a_subscriber_failure_goes_to_the_reporter_and_nothing_keeps_it() -> None:
    reports: list[HostReport] = []
    with Graph("subscriber", report=reports.append) as g:
        a = g.state(0)
        later: Arrays = []

        def failing(_: object) -> None:
            raise ValueError("sink failed")

        sub = a.subscribe(failing)
        a.subscribe(later.append)
        for v in range(40):
            a.set(v + 1)
        assert len(reports) == 41  # START and 40 waves; R-failure: no history is kept
        assert all(isinstance(r.error, SubscriberCallbackError) for r in reports)
        assert isinstance(reports[0].error.original, ValueError)  # type: ignore[attr-defined]
        assert reports[0].source.startswith("sink ")
        assert not hasattr(sub, "callback_errors")
        assert a.status.output == "open" and later[-1][-1] == ("DATA", 40)


def test_without_a_reporter_a_subscriber_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with Graph("logged") as g:
        a = g.state(0)

        def failing(_: object) -> None:
            raise ValueError("sink failed")

        with caplog.at_level(logging.ERROR, logger="graphrefly"):
            a.subscribe(failing)
        assert [r.getMessage() for r in caplog.records] == [
            "graphrefly sink sink#1 failed: SubscriberCallbackError('sink failed')"
        ]


@pytest.mark.parametrize("fatal", [KeyboardInterrupt, SystemExit])
def test_a_fatal_in_a_subscriber_propagates_and_poisons_the_facade(
    fatal: type[BaseException],
) -> None:
    g = Graph("fatal-sink")
    a = g.state(0)

    def sink(array: tuple[Message[int], ...]) -> None:
        if array[0] == ("PENDING",):
            raise fatal

    sub = a.subscribe(sink)
    with pytest.raises(fatal):
        a.set(1)
    assert g.closed and sub.closed
    with pytest.raises(GraphReflyRuntimeError, match="fatal"):
        a.cache()


@pytest.mark.parametrize("fatal", [KeyboardInterrupt, RecursionError, MemoryError])
@pytest.mark.parametrize("where", ["callback", "setup", "batch-commit", "cleanup"])
def test_a_fatal_from_a_callback_propagates_without_a_graph_error(
    where: str, fatal: type[BaseException]
) -> None:
    g = Graph("fatal")
    a = g.state(0, name="a")
    armed = [False]

    def boom() -> None:
        if armed[0]:
            armed[0] = False
            raise fatal

    def setup(ctx: Ctx) -> Any:
        if where == "setup":
            boom()
        return (lambda: boom()) if where == "cleanup" else None

    def n(ctx: Ctx) -> None:
        if where in ("callback", "batch-commit"):
            boom()
        ctx.emit(ctx.deps[0].value)

    node = g.node([a], n, setup=setup)
    seen: Arrays = []
    armed[0] = where == "setup"
    if where == "setup":
        with pytest.raises(fatal):
            node.subscribe(seen.append)
    else:
        sub = node.subscribe(seen.append)
        armed[0] = True
        with pytest.raises(fatal):
            if where == "callback":
                a.set(1)
            elif where == "batch-commit":
                g.batch(lambda: a.set(1))
            else:
                sub.unsubscribe()
    assert g.closed
    assert not any(m[0] == "ERROR" for array in seen for m in array)


# ---- host lifetime -----------------------------------------------------------------------


def test_close_releases_subscriptions_and_rejects_later_use() -> None:
    g = Graph("close")
    a = g.state(1, name="a")
    sub = a.subscribe(lambda _: None)
    g.close()
    g.close()
    assert g.closed and sub.closed
    sub.unsubscribe()
    with pytest.raises(GraphReflyRuntimeError, match="graph is closed"):
        a.cache()
    with pytest.raises(GraphReflyRuntimeError, match="graph is closed"):
        g.state(2)


def test_a_dropped_subscription_handle_stays_subscribed_until_close() -> None:
    g = Graph("drop")
    a = g.state(1, name="a")
    seen: Arrays = []
    a.subscribe(seen.append)
    gc.collect()
    a.set(2)
    assert data(seen) == [1, 2]
    g.close()
    assert g.closed


def test_retain_is_a_subscription_that_ignores_what_it_receives() -> None:
    with Graph("retain") as g:
        a = g.state(1)
        runs: list[int] = []
        d = g.derived([a], lambda v: runs.append(v) or v)
        keepalive = g.retain(d)
        gc.collect()
        a.set(2)
        assert runs == [1, 2] and d.cache() == 2
        assert isinstance(keepalive, Subscription)
        keepalive.unsubscribe()
        keepalive.unsubscribe()  # idempotent
        a.set(3)
        assert runs == [1, 2] and keepalive.closed
        assert d.status.lifecycle == "inactive"
        with g.retain(d) as scoped:
            assert not scoped.closed and d.status.lifecycle == "active"
        assert scoped.closed and d.status.lifecycle == "inactive"
        held = g.retain(d)
        g.close()  # a retain is closed with the graph's other subscriptions
        assert held.closed
    other = Graph("other")
    with pytest.raises(GraphReflyRuntimeError, match="node must belong"):
        other.retain(d)
    other.close()


def test_close_uses_the_reverse_resource_stack_and_continues_after_an_error() -> None:
    g = Graph("stack")
    order: list[str] = []

    class Resource:
        def __init__(self, label: str, raises: bool = False) -> None:
            self.label = label
            self.raises = raises

        def _close_from_graph(self) -> None:
            order.append(self.label)
            if self.raises:
                raise RuntimeError(self.label)

    for label, raises in (("first", False), ("second", False), ("third", True)):
        g._lifetime.register_resource(Resource(label, raises))
    with pytest.raises(RuntimeError, match="third"):
        g.close()
    assert order == ["third", "second", "first"]


def test_a_graph_is_bound_to_its_owner_thread() -> None:
    g = Graph("thread")
    a = g.state(1)
    errors: list[BaseException] = []

    def other() -> None:
        try:
            a.cache()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=other)
    thread.start()
    thread.join()
    assert len(errors) == 1 and isinstance(errors[0], GraphReflyRuntimeError)
    g.close()


def test_find_returns_the_nodes_own_handle_and_a_state_as_state() -> None:
    with Graph("find") as g:
        a = g.state(1, name="a")
        d = g.derived([a], lambda v: v, name="d")
        assert g.find("a") is a and isinstance(g.find("a"), State)
        assert g.find("d") is d and not isinstance(g.find("d"), State)
        assert g.find("missing") is None
        assert d.status.lifecycle == "inactive"  # find activates nothing (R-graph-surface)


def test_node_names_are_unique_and_auto_names_are_reserved() -> None:
    with Graph("names") as g:
        g.state(1, name="a")
        with pytest.raises(GraphReflyValueError, match="already used"):
            g.state(2, name="a")
        with pytest.raises(GraphReflyValueError, match="#"):
            g.state(2, name="#1")
        assert g.state(3).name.startswith("#")


# ---- checkpoint and restore ---------------------------------------------------------------


def _registry() -> dict[str, Any]:
    def total(config: object, at: RecipeContext) -> Any:
        def run(ctx: Ctx) -> None:
            ctx.state = (ctx.state or 0) + 1
            ctx.emit(sum(d.value if d.has else 0 for d in ctx.deps) + (config or 0))

        return run

    return {"total": total}


def _no_make(ref: str, config: object, name: str) -> Any:
    raise AssertionError("not restoring")


def test_checkpoint_and_restore_graph_round_trip() -> None:
    reg = _registry()
    g = Graph("cp")
    a = g.state(1, name="a")
    built = reg["total"](100, RecipeContext(g, "t", _no_make))
    t = g.node([a], built, name="t", recipe=Recipe("total", 100))
    t.subscribe(lambda _: None)
    a.set(2)
    cp = g.checkpoint()
    r = graphrefly.restore_graph(cp, registry=reg)
    assert r.describe() == g.describe()
    restored = r.find("t")
    assert restored is not None and not isinstance(restored, State)
    seen: Arrays = []
    restored.subscribe(seen.append)
    assert seen == [(("START",), ("DATA", 102))]
    state = r.find("a")
    assert isinstance(state, State)  # a restored state is writable through its State
    state.set(5)
    assert restored.cache() == 105
    g.close()
    r.close()


def test_checkpoint_refuses_non_json_and_restore_refuses_local_only_nodes() -> None:
    with Graph("cp-errors") as g:
        g.state((1, 2), name="tuple")
        with pytest.raises(GraphReflyCheckpointError, match="not JSON"):
            g.checkpoint()
    with Graph("local") as g:
        a = g.state(1)
        g.node([a], lambda ctx: None)
        cp = g.checkpoint()
        with pytest.raises(GraphReflyRestoreError, match="local-only"):
            graphrefly.restore_graph(cp, registry={})
        with pytest.raises(GraphReflyRestoreError, match="checkpoint@1"):
            graphrefly.restore_graph({"nodes": []}, registry={})


def test_callback_error_is_public_taxonomy() -> None:
    assert issubclass(CallbackError, graphrefly.GraphReflyError)
    assert issubclass(GraphCallbackError, CallbackError)
    assert issubclass(SubscriberCallbackError, CallbackError)


# ---- review fixes: close, definition, recipes and set(fn) -----------------------------


def test_a_cleanup_that_graph_close_runs_can_still_use_its_ctx() -> None:
    reports: list[HostReport] = []
    g = Graph("close", report=reports.append)
    a = g.state(1)
    read: list[object] = []

    def setup(ctx: Ctx) -> Any:
        ctx.state = "kept"
        return lambda: read.append(ctx.state)

    g.node([a], lambda ctx: None, setup=setup).subscribe(lambda _: None)
    g.close()
    assert read == ["kept"] and reports == []  # R-lifetime: the cleanup reads its state


def test_definition_follows_the_instance_the_kernel_runs() -> None:
    reports: list[HostReport] = []
    g = Graph("definition", report=reports.append)
    a = g.state(1, name="a")
    b = g.state(2, name="b")

    def old(ctx: Ctx) -> None:
        ctx.emit("old")

    def new(ctx: Ctx) -> None:
        ctx.emit("new")

    n = g.node([a], old, name="n")
    m = g.node([a], old, name="m")
    n.subscribe(lambda _: None)

    def body() -> None:
        g.replace(n, [b], new)
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        g.batch(body)  # a body that throws drops its replace (R-request-order)
    assert g.definition(n).callback is old and n.cache() == "old"

    def cycle() -> None:
        g.replace(n, [m], new)
        g.replace(m, [n], new)  # valid when asked for, refused when it runs

    g.batch(cycle)
    assert g.definition(n).callback is new and g.definition(n).deps == (m,)
    assert g.definition(m).callback is old and g.definition(m).deps == (a,)
    assert [r.source for r in reports] == ["queued request"]


def test_a_recipe_factory_can_make_another_registered_recipe() -> None:
    def plus(config: Any, at: RecipeContext) -> Any:
        return lambda ctx: ctx.emit(ctx.deps[0].value + config)

    def twice(config: Any, at: RecipeContext) -> Any:
        inner, _setup = at.make("plus", config, at.name)
        return inner

    reg: dict[str, Any] = {"plus": plus, "twice": twice}
    g = Graph("make")

    def make(ref: str, config: object, name: str) -> Any:
        return reg[ref](config, RecipeContext(g, name, make)), None

    a = g.state(1, name="a")
    t = g.node([a], twice(5, RecipeContext(g, "t", make)), name="t", recipe=Recipe("twice", 5))
    t.subscribe(lambda _: None)
    r = graphrefly.restore_graph(g.checkpoint(), registry=reg)
    restored = r.find("t")
    assert restored is not None
    restored.subscribe(lambda _: None)
    assert restored.cache() == 6
    callback = r.definition(restored).callback
    assert callback is not None and callable(callback)


def test_set_takes_functions_methods_and_partials_as_updaters_others_as_values() -> None:
    g = Graph("set")
    s = g.state(0)
    s.set(str)  # a class is a value, as Graph.state(str) takes it
    assert s.cache() is str

    class Callable_:
        def __call__(self, value: object) -> object:
            return "called"

    instance = Callable_()
    s.set(instance)
    assert s.cache() is instance
    s.set(lambda _: 2)
    s.set(functools.partial(operator.add, 3))
    assert s.cache() == 5
    t = g.state("ab")
    t.set(str.upper)  # a method
    assert t.cache() == "AB"


@pytest.mark.parametrize(
    ("start", "updater"),
    [
        (5, lambda v: v + 0),
        (1000, lambda v: v + 0),  # CPython caches small ints only: no difference
        (1.5, lambda v: v + 0.0),
        (float("nan"), lambda v: v * 1.0),
        ("text", lambda v: "".join(list(v))),
        (b"x", lambda v: bytes(bytearray(v))),
        (True, lambda v: bool(v)),
    ],
)
def test_an_updater_returning_an_equal_scalar_writes_nothing(
    start: object, updater: Callable[[Any], object]
) -> None:
    g = Graph("same")
    s = g.state(start)
    seen: Arrays = []
    s.subscribe(seen.append)
    s.set(updater)
    assert len(seen) == 1  # only START


def test_an_updater_returning_minus_zero_for_zero_or_an_equal_list_writes() -> None:
    g = Graph("differ")
    s = g.state(0.0)
    seen: Arrays = []
    s.subscribe(seen.append)
    s.set(lambda value: -value)
    assert seen[-1][-1] == ("DATA", -0.0)
    s.set(lambda value: 1)
    s.set(lambda value: True)  # True is not the int 1
    assert seen[-1][-1] == ("DATA", True)
    lst = g.state([1])
    seen.clear()
    lst.subscribe(seen.append)
    lst.set(lambda value: list(value))  # an equal list is a new value
    assert len(seen) == 2


def test_derived_and_effect_keep_what_arrived_while_their_gate_was_closed() -> None:
    # Q1 of the D797 review: the first-run gate lives in derived and effect, and
    # inputs that arrive while it is closed are kept, not dropped.
    g = Graph("gate")
    a = g.state(0, name="a")
    b = g.state(name="b")  # empty: the gate stays closed
    runs: list[tuple[object, object]] = []
    acted: list[tuple[object, object]] = []

    def pair(x: object, y: object) -> tuple[object, object]:
        runs.append((x, y))
        return (x, y)

    d = g.derived([a, b], pair)
    e = g.effect([a, b], lambda x, y: acted.append((x, y)))
    d.subscribe(lambda _: None)
    e.subscribe(lambda _: None)
    a.set(1)
    a.set(2)  # arrives while gated
    assert runs == [] and acted == []
    b.set("x")  # only b sends in this wave; a's latest value is still there
    assert runs == [(2, "x")] and acted == [(2, "x")]
    assert d.cache() == (2, "x")
    raw: list[list[list[object]]] = []
    n = g.node([a, b], lambda ctx: raw.append([dep.waves for dep in ctx.deps]))
    n.subscribe(lambda _: None)
    assert raw == [[[[2]], [["x"]]]]  # Graph.node has no gate (graphrefly-py:D4)
