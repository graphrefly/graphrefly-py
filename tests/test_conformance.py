import pytest

import graphrefly
from graphrefly import (
    SENTINEL,
    CallbackError,
    ControlMessage,
    DataMessage,
    ErrorMessage,
    Graph,
    GraphReflyRuntimeError,
    GraphReflyValueError,
    Message,
    PullContext,
    Sentinel,
)
from graphrefly import _conformance as conformance
from graphrefly._conformance import ConformanceStimulus


def test_c5_pause_lockset_multi_source_public_facade():
    graph = Graph("py-c5-pause-lockset")
    source = graph.state(0, name="source")
    runs = 0

    def passthrough(value: int) -> int:
        nonlocal runs
        runs += 1
        return value

    node = graph.derived([source], passthrough, name="node")

    with node.subscribe(lambda _msg: None):
        assert node.cache() == 0
        runs = 0

        graph.pause(node, "A")
        graph.pause(node, "B")
        graph.pause(node, "A")

        source.set(1)
        assert runs == 0
        assert node.cache() == 0

        graph.resume(node, "A")
        assert runs == 0
        assert node.cache() == 0

        graph.resume(node, "unknown")
        assert runs == 0

        graph.resume(node, "B")
        assert runs == 1
        assert node.cache() == 1


def test_c8_private_immediate_rewire_surgical_and_drained():
    graph = Graph("py-c8-immediate-rewire")
    stimulus = ConformanceStimulus(graph)
    source_a = graph.state(1, name="source-a")
    source_b = graph.state(100, name="source-b")
    runs = 0

    def a_only(ctx) -> None:
        nonlocal runs
        runs += 1
        ctx.emit(ctx.data(0))

    node = graph.node([source_a], a_only, name="rewired")

    with node.subscribe(lambda _msg: None):
        assert node.cache() == 1

        def sum_deps(ctx) -> None:
            nonlocal runs
            runs += 1
            ctx.emit(ctx.data(0) + ctx.data(1))

        stimulus.c8_immediate_subscribe_dep(node, source_b, sum_deps)
        assert node.cache() == 101

        source_b.set(50)
        assert node.cache() == 51

        before_unsubscribe_runs = runs

        def b_only(ctx) -> None:
            nonlocal runs
            runs += 1
            ctx.emit(ctx.data(0))

        stimulus.c8_immediate_unsubscribe_dep(node, source_a, b_only)
        assert node.cache() == 51
        assert runs == before_unsubscribe_runs

        source_a.set(9)
        assert node.cache() == 51

        source_b.set(7)
        assert node.cache() == 7

        replace_runs = 0

        def replacement(ctx) -> None:
            nonlocal replace_runs
            replace_runs += 1
            ctx.emit(ctx.data(0))

        stimulus.c8_immediate_replace_deps(node, [source_b], replacement)
        assert replace_runs == 0
        assert node.cache() == 7


def test_c8_private_immediate_rewire_with_sentinel_dep_waits_for_data():
    graph = Graph("py-c8-sentinel-rewire")
    stimulus = ConformanceStimulus(graph)
    source = graph.state(1, name="source")
    empty = stimulus.state_empty("empty")
    runs = 0

    def body(ctx) -> None:
        nonlocal runs
        runs += 1
        if ctx.dep_len == 2 and ctx.has_data(1):
            ctx.emit(ctx.data(0) + ctx.data(1))
            return
        ctx.emit(ctx.data(0))

    node = graph.node([source], body, name="sentinel-rewire")
    with node.subscribe(lambda _msg: None):
        assert node.cache() == 1
        runs = 0
        stimulus.c8_immediate_subscribe_dep(node, empty, body)
        assert runs == 0
        assert node.cache() == 1

        empty.set(10)
        assert node.cache() == 11
        assert runs == 1


def test_c8_private_immediate_rewire_rejects_invalid_boundaries():
    graph = Graph("py-c8-rewire-invalid")
    stimulus = ConformanceStimulus(graph)
    other_graph = Graph("py-c8-rewire-other")
    source = graph.state(1, name="source")
    helper = graph.state(2, name="helper")
    foreign = other_graph.state(3, name="foreign")
    node = graph.node([source], lambda ctx: ctx.emit(ctx.data(0)), name="node")

    with pytest.raises(GraphReflyValueError, match="graphrefly.Node"):
        stimulus.c8_immediate_subscribe_dep(  # type: ignore[arg-type]
            node,
            object(),
            lambda ctx: ctx.emit(None),
        )

    with pytest.raises(GraphReflyRuntimeError, match="belong to this GraphReFly graph"):
        stimulus.c8_immediate_subscribe_dep(foreign, helper, lambda ctx: ctx.emit(ctx.data(0)))

    with pytest.raises(GraphReflyValueError, match="callback must be callable"):
        stimulus.c8_immediate_subscribe_dep(node, helper, object())  # type: ignore[arg-type]

    async def async_body(_ctx) -> None:
        return None

    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        stimulus.c8_immediate_subscribe_dep(node, helper, async_body)


def test_c8_private_immediate_rewire_on_terminal_node_rejected():
    graph = Graph("py-c8-terminal-rewire")
    stimulus = ConformanceStimulus(graph)
    source = graph.state(1, name="source")
    node = graph.node([source], lambda ctx: ctx.emit(ctx.data(0)), name="terminal")

    with node.subscribe(lambda _msg: None):
        stimulus.c23_dep_completes(node)
        assert node.status == "completed"

        with pytest.raises(GraphReflyRuntimeError, match="terminal"):
            stimulus.c8_immediate_replace_deps(
                node,
                [source],
                lambda ctx: ctx.emit(ctx.data(0)),
            )


def test_c8_private_immediate_rewire_fatal_callback_poisons_facade():
    graph = Graph("py-c8-fatal-rewire")
    stimulus = ConformanceStimulus(graph)
    source = graph.state(1, name="source")
    helper = graph.state(2, name="helper")
    seen: list[Message[object]] = []
    node = graph.node([source], lambda ctx: ctx.emit(ctx.data(0)), name="fatal-rewire")

    def fatal(_ctx) -> None:
        raise SystemExit("c8 rewire exit")

    with pytest.raises(SystemExit, match="c8 rewire exit"), node.subscribe(seen.append):
        stimulus.c8_immediate_subscribe_dep(node, helper, fatal)

    assert graph.closed is True
    assert not any(isinstance(message, ErrorMessage) for message in seen)
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        node.cache()


def test_c8_immediate_rewire_remains_private_not_public_node_facade():
    graph = Graph("py-c8-private-boundary")
    source = graph.state(1, name="source")
    node = graph.node([source], lambda ctx: ctx.emit(ctx.data(0)), name="node")

    assert not hasattr(node, "subscribe_dep")
    assert not hasattr(node, "unsubscribe_dep")
    assert not hasattr(node, "replace_deps")


def test_c2_async_result_at_paused_node_private_harness():
    graph = Graph("py-c2-async-paused")
    stimulus = ConformanceStimulus(graph)
    trigger = graph.state(0, name="trigger")
    node, pending = stimulus.c2_async_result_node(trigger, name="async-compute")
    seen: list[Message[object]] = []

    with node.subscribe(seen.append):
        assert pending.has_pending()
        assert node.has_value is False

        graph.pause(node, "L1")
        seen.clear()
        pending.resolve(42)

        assert seen == []
        assert node.has_value is False

        graph.resume(node, "L1")
        assert _kinds(seen) == ["DIRTY", "DATA"]
        assert _data_values(seen) == [42]
        assert node.cache() == 42


def test_c4_mixed_sync_async_diamond_private_harness():
    graph = Graph("py-c4-mixed-diamond")
    stimulus = ConformanceStimulus(graph)
    source = graph.state(1, name="source")
    sync_leg = graph.derived([source], lambda value: value + 10, name="sync-leg")
    async_leg, pending = stimulus.c4_async_diamond_leg(source, name="async-leg")
    runs = 0
    seen: list[Message[object]] = []

    def join(left: int, right: int) -> int:
        nonlocal runs
        runs += 1
        return left + right

    diamond = graph.derived([sync_leg, async_leg], join, name="diamond")
    with diamond.subscribe(seen.append):
        assert pending.has_pending()
        assert runs == 0
        assert diamond.has_value is False

        pending.resolve(21)
        assert runs == 1
        assert diamond.cache() == 32

        seen.clear()
        source.set(2)
        assert pending.has_pending()
        assert runs == 1

        pending.resolve(30)
        assert runs == 2
        assert diamond.cache() == 42
        assert _kinds(seen) == ["DIRTY", "DATA"]
        assert _data_values(seen) == [42]


def test_c9_pausable_false_async_source_ignores_pause_private_harness():
    graph = Graph("py-c9-pausable-false-async-source")
    stimulus = ConformanceStimulus(graph)
    source, pending = stimulus.c9_pausable_false_async_source(name="async-source")
    seen: list[Message[object]] = []

    with source.subscribe(seen.append):
        assert pending.has_pending()
        graph.pause(source, "L1")
        seen.clear()

        pending.resolve(42)
        assert _kinds(seen) == ["DIRTY", "DATA"]
        assert _data_values(seen) == [42]
        assert source.cache() == 42

        graph.resume(source, "L1")
        assert _kinds(seen) == ["DIRTY", "DATA"]


def test_c10_true_mode_async_leaf_source_delivers_immediately_private_harness():
    graph = Graph("py-c10-true-async-leaf")
    stimulus = ConformanceStimulus(graph)
    source, pending = stimulus.c10_true_mode_async_leaf_source(name="async-leaf")
    seen: list[Message[object]] = []

    with source.subscribe(seen.append):
        assert pending.has_pending()
        graph.pause(source, "L1")
        seen.clear()

        pending.resolve(7)
        assert _kinds(seen) == ["DIRTY", "DATA"]
        assert _data_values(seen) == [7]
        assert source.cache() == 7


def test_c21_late_async_ctx_emission_uses_live_deps_after_rewire_private_harness():
    graph = Graph("py-c21-live-edge")
    stimulus = ConformanceStimulus(graph)
    stale_dep = graph.state(1, name="stale")
    live_dep = graph.state(2, name="live")
    node, pending = stimulus.c21_live_edge_async_node(stale_dep, name="async-node")

    with node.subscribe(lambda _msg: None):
        assert pending.has_pending()
        stimulus.c21_replace_with_live_dep(node, live_dep, pending)
        pending.invalidate_live_deps()

        assert stale_dep.has_value is True
        assert stale_dep.cache() == 1
        assert live_dep.has_value is False
        assert pending.has_pending() is False

        live_dep.set(3)
        assert pending.has_pending()
        pending.resolve(9)
        assert node.cache() == 9

    terminal_graph = Graph("py-c21-terminal-guard")
    terminal_stimulus = ConformanceStimulus(terminal_graph)
    terminal_dep = terminal_graph.state(1, name="dep")
    terminal_node, terminal_pending = terminal_stimulus.c21_live_edge_async_node(
        terminal_dep,
        name="async-terminal",
    )
    terminal_seen: list[Message[object]] = []

    with terminal_node.subscribe(terminal_seen.append):
        assert terminal_pending.has_pending()
        terminal_stimulus.c23_dep_completes(terminal_node)
        assert terminal_node.status == "completed"
        terminal_seen.clear()

        terminal_pending.resolve("late")
        assert terminal_seen == []
        assert terminal_node.has_value is False


def test_c22_private_immediate_rewire_waits_for_batch_commit_old_shape_first():
    graph = Graph("py-c22-batch-before-immediate-rewire")
    stimulus = ConformanceStimulus(graph)
    source_a = graph.state(1, name="a")
    source_b = graph.state(10, name="b")
    seen: list[Message[object]] = []

    def a_body(ctx) -> None:
        ctx.emit(ctx.data(0))

    node = graph.node([source_a], a_body, name="node")

    with node.subscribe(seen.append):
        assert node.cache() == 1
        seen.clear()

        def b_body(ctx) -> None:
            ctx.emit(ctx.data(0) * 10)

        def batch_body() -> None:
            stimulus.c22_old_shape_data(node, 2)
            stimulus.c8_immediate_replace_deps(node, [source_b], b_body)

            assert _data_values(seen) == []
            assert node.cache() == 1

        graph.batch(batch_body)

        assert _kinds(seen) == ["DIRTY", "DATA", "DIRTY", "DATA"]
        assert _data_values(seen) == [2, 100]
        assert node.cache() == 100

        seen.clear()
        source_a.set(3)
        assert _data_values(seen) == []

        source_b.set(11)
        assert _data_values(seen) == [110]


def test_c22_private_immediate_rewire_rollback_discards_queued_topology_changes():
    graph = Graph("py-c22-rollback-immediate-rewire")
    stimulus = ConformanceStimulus(graph)
    source = graph.state("idle", name="source")
    helper = graph.state("helper", name="helper")
    replacement = graph.state("replacement", name="replacement")
    seen: list[Message[object]] = []

    def source_only(ctx) -> None:
        ctx.emit(("source", ctx.data(0)))

    node = graph.node([source], source_only, name="node", partial=True)
    with node.subscribe(seen.append):
        seen.clear()

        def source_helper(ctx) -> None:
            for index in range(ctx.dep_len):
                for value in _fresh_values(ctx, index):
                    ctx.emit(("live", value))

        def rollback_subscribe() -> None:
            stimulus.c22_old_shape_data(node, ("old", "attach"))
            stimulus.c8_immediate_subscribe_dep(node, helper, source_helper)
            raise ValueError("rollback")

        with pytest.raises(ValueError, match="rollback"):
            graph.batch(rollback_subscribe)

        helper.set("must-not-drive")
        assert ("live", "must-not-drive") not in _data_values(seen)

        source.set("after")
        assert ("source", "after") in _data_values(seen)

    drop_seen: list[Message[object]] = []
    drop_source = graph.state("drop-idle", name="drop-source")
    live_helper = graph.state("live-helper", name="live-helper")

    def two_dep_body(ctx) -> None:
        for index in range(ctx.dep_len):
            for value in _fresh_values(ctx, index):
                ctx.emit(("dep", index, value))

    drop_node = graph.node([drop_source, live_helper], two_dep_body, name="drop-node", partial=True)
    with drop_node.subscribe(drop_seen.append):
        drop_seen.clear()

        def replacement_body(ctx) -> None:
            for index in range(ctx.dep_len):
                for value in _fresh_values(ctx, index):
                    ctx.emit(("replacement", index, value))

        def rollback_drop() -> None:
            stimulus.c22_old_shape_data(drop_node, ("old", "drop"))
            stimulus.c8_immediate_unsubscribe_dep(drop_node, live_helper, two_dep_body)
            stimulus.c8_immediate_replace_deps(
                drop_node,
                [drop_source, replacement],
                replacement_body,
            )
            raise ValueError("rollback")

        with pytest.raises(ValueError, match="rollback"):
            graph.batch(rollback_drop)

        drop_seen.clear()
        live_helper.set("still-live")
        replacement.set("must-not-drive")

        assert ("dep", 1, "still-live") in _data_values(drop_seen)
        assert ("replacement", 1, "must-not-drive") not in _data_values(drop_seen)


def test_c22_private_immediate_rewire_fifo_order_after_batch_commit():
    graph = Graph("py-c22-immediate-rewire-fifo")
    stimulus = ConformanceStimulus(graph)
    source_a = graph.state(1, name="a")
    source_b = graph.state(10, name="b")
    source_c = graph.state(100, name="c")
    seen: list[Message[object]] = []

    def a_body(ctx) -> None:
        ctx.emit(ctx.data(0))

    node = graph.node([source_a], a_body, name="node")

    with node.subscribe(seen.append):
        seen.clear()

        def b_body(ctx) -> None:
            ctx.emit(("b", ctx.data(0)))

        def c_body(ctx) -> None:
            ctx.emit(("c", ctx.data(0)))

        def batch_body() -> None:
            stimulus.c22_old_shape_data(node, 2)
            stimulus.c8_immediate_replace_deps(node, [source_b], b_body)
            stimulus.c8_immediate_replace_deps(node, [source_c], c_body)
            assert _data_values(seen) == []

        graph.batch(batch_body)

        assert _data_values(seen) == [2, ("b", 10), ("c", 100)]

        seen.clear()
        source_b.set(11)
        assert _data_values(seen) == []

        source_c.set(101)
        assert _data_values(seen) == [("c", 101)]


def test_c22_private_immediate_subscribe_dep_fifo_composes_after_batch_commit():
    graph = Graph("py-c22-immediate-subscribe-fifo")
    stimulus = ConformanceStimulus(graph)
    source = graph.state(1, name="source")
    helper_b = graph.state(10, name="helper-b")
    helper_c = graph.state(100, name="helper-c")
    seen: list[Message[object]] = []

    def source_body(ctx) -> None:
        ctx.emit(("source", ctx.data(0)))

    node = graph.node([source], source_body, name="node", partial=True)

    with node.subscribe(seen.append):
        seen.clear()

        def b_body(ctx) -> None:
            for index in range(ctx.dep_len):
                for value in _fresh_values(ctx, index):
                    ctx.emit((index, value))

        def c_body(ctx) -> None:
            for index in range(ctx.dep_len):
                for value in _fresh_values(ctx, index):
                    ctx.emit((index, value))

        def batch_body() -> None:
            stimulus.c22_old_shape_data(node, ("old", "batch"))
            stimulus.c8_immediate_subscribe_dep(node, helper_b, b_body)
            stimulus.c8_immediate_subscribe_dep(node, helper_c, c_body)
            assert _data_values(seen) == []

        graph.batch(batch_body)

        assert _data_values(seen) == [
            ("old", "batch"),
            (1, 10),
            (2, 100),
        ]

        seen.clear()
        helper_b.set(11)
        helper_c.set(101)
        assert _data_values(seen) == [(1, 11), (2, 101)]


def test_c6_synchronous_feedback_cycle_becomes_graph_error_without_recursion():
    graph = Graph("py-c6-feedback")
    source = graph.state(0, name="source")
    derived = graph.derived([source], lambda value: value + 1, name="derived")
    effect_runs: list[int] = []
    seen: list[Message[object]] = []

    def feedback(value: int) -> None:
        effect_runs.append(value)
        source.set(value)

    effect = graph.effect([derived], feedback, name="effect")

    with effect.subscribe(seen.append):
        errors = [msg for msg in seen if isinstance(msg, ErrorMessage)]
        assert effect_runs == [1]
        assert errors
        assert "synchronous feedback cycle" in errors[-1].error.message
        assert effect.status == "errored"

        source.set(10)
        assert source.cache() == 10
        assert derived.cache() == 11
        assert effect_runs == [1]


def test_c6_synchronous_feedback_cycle_does_not_escape_as_python_exception():
    graph = Graph("py-c6-feedback-boundary")
    source = graph.state(0, name="source")
    derived = graph.derived([source], lambda value: value + 1, name="derived")

    def feedback(value: int) -> None:
        source.set(value)

    effect = graph.effect([derived], feedback, name="effect")

    with effect.subscribe(lambda _msg: None):
        assert effect.status == "errored"


def test_c7_public_invalidate_upstream_control_at_depless_source():
    graph = Graph("py-c7-upstream-invalidate")
    source = graph.state(5, name="source")
    derived = graph.derived([source], lambda value: value, name="derived")
    seen: list[Message[object]] = []

    with derived.subscribe(seen.append):
        seen.clear()
        graph.invalidate(derived)

        assert source.has_value is False
        assert derived.has_value is False
        assert ControlMessage("INVALIDATE") in seen


def test_c3_invalidate_preserves_ctx_state_and_fires_current_hook_once():
    graph = Graph("py-c3-invalidate-state-hook")
    source = graph.state(1, name="source")
    flushes = 0
    states_seen: list[object | None] = []
    seen: list[Message[object]] = []

    def body(ctx) -> None:
        nonlocal flushes
        states_seen.append(ctx.state)
        ctx.state = (ctx.state or 0) + 1
        ctx.on_invalidate(lambda: _increment_flush())
        ctx.emit(ctx.data(0) * 2)

    def _increment_flush() -> None:
        nonlocal flushes
        flushes += 1

    node = graph.node([source], body, name="node")

    with node.subscribe(seen.append):
        assert node.cache() == 2
        seen.clear()

        graph.invalidate(source)
        graph.invalidate(source)

        assert flushes == 1
        assert node.has_value is False
        assert seen.count(ControlMessage("INVALIDATE")) == 1

        source.set(3)
        assert node.cache() == 6

    assert states_seen == [None, 1]


def test_c7_invalidate_upstream_source_and_downstream_hooks_fire_once():
    graph = Graph("py-c7-upstream-invalidate-hooks")
    source_flushes = 0
    derived_flushes = 0
    seen: list[Message[object]] = []

    def source_body(ctx) -> None:
        nonlocal source_flushes
        ctx.on_invalidate(lambda: _source_flush())
        ctx.emit(5)

    def derived_body(ctx) -> None:
        nonlocal derived_flushes
        ctx.on_invalidate(lambda: _derived_flush())
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    def _source_flush() -> None:
        nonlocal source_flushes
        source_flushes += 1

    def _derived_flush() -> None:
        nonlocal derived_flushes
        derived_flushes += 1

    source = graph.node([], source_body, name="source")
    derived = graph.node([source], derived_body, name="derived")

    with derived.subscribe(seen.append):
        seen.clear()
        graph.invalidate(derived)

        assert source_flushes == 1
        assert derived_flushes == 1
        assert source.has_value is False
        assert derived.has_value is False
        assert seen.count(ControlMessage("INVALIDATE")) == 1


def test_c7_private_native_negative_upstream_controls_drop_at_depless_source():
    graph = Graph("py-c7-native-negative-controls")
    stimulus = ConformanceStimulus(graph)
    source = graph.state(5, name="source")
    derived = graph.derived([source], lambda value: value, name="derived")
    seen: list[Message[object]] = []

    with derived.subscribe(seen.append):
        assert derived.cache() == 5
        seen.clear()

        stimulus.c7_send_unheld_dirty_up(derived)
        assert source.cache() == 5
        assert source.status == "settled"
        assert seen == []

        stimulus.c7_send_unheld_teardown_up(derived)
        assert source.cache() == 5
        assert source.status == "settled"
        assert seen == []


def test_c13_paused_invalidate_sole_dep_cancel_public_facade_regression():
    graph = Graph("py-c13-sole-dep-cancel")
    dep = graph.state(0, name="dep")
    runs = 0
    seen: list[Message[object]] = []

    def plus_one(value: int) -> int:
        nonlocal runs
        runs += 1
        return value + 1

    node = graph.derived([dep], plus_one, name="node")
    with node.subscribe(seen.append):
        assert node.cache() == 1
        runs = 0
        seen.clear()

        graph.pause(node, "p")
        dep.set(5)
        assert runs == 0
        graph.invalidate(dep)
        graph.resume(node, "p")

        assert runs == 0
        assert node.status != "errored"
        assert not any(isinstance(msg, ErrorMessage) for msg in seen)
        assert node.has_value is False


def test_c13_paused_invalidate_rearm_public_facade_regression():
    graph = Graph("py-c13-rearm")
    dep = graph.state(0, name="dep")
    runs = 0

    def plus_hundred(value: int) -> int:
        nonlocal runs
        runs += 1
        return value + 100

    node = graph.derived([dep], plus_hundred, name="node")
    with node.subscribe(lambda _msg: None):
        runs = 0

        graph.pause(node, "p")
        dep.set(5)
        graph.invalidate(dep)
        dep.set(7)
        graph.resume(node, "p")

        assert runs == 1
        assert node.cache() == 107


def test_c13_paused_invalidate_multi_dep_sentinel_guard_survives_other_dep():
    graph = Graph("py-c13-multi-dep-sentinel-guard")
    d1 = graph.state(1, name="d1")
    d2 = graph.state(2, name="d2")
    runs = 0
    values_seen: list[tuple[object, object]] = []

    def combine(ctx) -> None:
        nonlocal runs
        runs += 1
        left = ctx.data(0, "SENTINEL")
        right = ctx.data(1, "SENTINEL")
        values_seen.append((left, right))
        if ctx.has_data(1):
            ctx.emit(("right", ctx.data(1)))

    node = graph.node([d1, d2], combine, name="node")
    with node.subscribe(lambda _msg: None):
        assert node.cache() == ("right", 2)
        runs = 0
        values_seen.clear()

        graph.pause(node, "p")
        d1.set(10)
        d2.set(20)
        graph.invalidate(d1)
        graph.resume(node, "p")

        assert runs == 1
        assert values_seen == [("SENTINEL", 20)]
        assert node.cache() == ("right", 20)


def test_c14_cleanup_hooks_are_per_run_and_single_run_hook_is_kept():
    graph = Graph("py-c14-cleanup-hooks")
    source = graph.state(1, name="source")
    flushes = 0
    cleanups = 0

    def body(ctx) -> None:
        nonlocal flushes, cleanups
        ctx.on_invalidate(lambda: _flush())
        ctx.on_deactivation(lambda: _cleanup())
        ctx.emit(ctx.data(0))

    def _flush() -> None:
        nonlocal flushes
        flushes += 1

    def _cleanup() -> None:
        nonlocal cleanups
        cleanups += 1

    node = graph.node([source], body, name="node")
    sub = node.subscribe(lambda _msg: None)
    source.set(2)
    source.set(3)
    graph.invalidate(source)
    sub.unsubscribe()

    assert flushes == 1
    assert cleanups == 1

    single_graph = Graph("py-c14-single-run-hook")
    single_source = single_graph.state(1, name="source")
    single_flushes = 0
    single_cleanups = 0

    def single_body(ctx) -> None:
        nonlocal single_flushes, single_cleanups
        ctx.on_invalidate(lambda: _single_flush())
        ctx.on_deactivation(lambda: _single_cleanup())
        ctx.emit(ctx.data(0))

    def _single_flush() -> None:
        nonlocal single_flushes
        single_flushes += 1

    def _single_cleanup() -> None:
        nonlocal single_cleanups
        single_cleanups += 1

    single_node = single_graph.node([single_source], single_body, name="node")
    single_sub = single_node.subscribe(lambda _msg: None)
    single_graph.invalidate(single_source)
    single_sub.unsubscribe()

    assert single_flushes == 1
    assert single_cleanups == 1


def _kinds(messages: list[Message[object]]) -> list[str]:
    return [message.kind for message in messages]


def _fresh_values(ctx, index: int) -> list[object]:
    return [
        value
        for wave in ctx.wave_data[index]
        for value in wave
        if value is not SENTINEL
    ]


def _data_values(messages: list[Message[object]]) -> list[object]:
    return [message.value for message in messages if isinstance(message, DataMessage)]


def test_c12_occurrences_stay_data_and_state_set_same_value_is_data():
    graph = Graph("py-c12-occurrences-data")
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    with source.subscribe(seen.append):
        seen.clear()

        source.set(1)
        source.set(1)
        source.set(1)

    assert _kinds(seen) == ["DIRTY", "DATA", "DIRTY", "DATA", "DIRTY", "DATA"]
    assert _data_values(seen) == [1, 1, 1]
    assert "RESOLVED" not in _kinds(seen)
    assert source.cache() == 1


def test_c12_take_style_counts_occurrences_not_distinct_values():
    graph = Graph("py-c12-take-occurrences")
    stimulus = ConformanceStimulus(graph)
    source = stimulus.state_empty("source")
    taken_seen: list[Message[object]] = []
    taken_values: list[object] = []

    def take_three(ctx) -> None:
        count = int(ctx.state) if ctx.has_state else 0
        for value in _fresh_values(ctx, 0):
            if count >= 3:
                break
            count += 1
            ctx.state = count
            ctx.emit(value)
            if count == 3:
                conformance.down_complete(ctx)
                break

    taken = graph.node([source], take_three, name="take-three")
    with taken.subscribe(taken_seen.append):
        taken_values.clear()
        taken_seen.clear()

        source.set(1)
        source.set(1)
        source.set(1)
        source.set(1)
        taken_values.extend(_data_values(taken_seen))
        assert taken.status == "completed"

    assert taken_values == [1, 1, 1]


def test_c12_filter_reject_synthesizes_undirty_resolved_no_wedge():
    graph = Graph("py-c12-filter-resolved")
    source = graph.state(50, name="source")
    seen: list[Message[object]] = []

    def only_large(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            if int(value) >= 100:
                ctx.emit(value)

    filtered = graph.node([source], only_large, name="only-large")
    with filtered.subscribe(seen.append):
        assert filtered.has_value is False
        assert filtered.status == "sentinel"
        seen.clear()

        source.set(60)
        assert _kinds(seen) == ["DIRTY", "RESOLVED"]
        assert filtered.has_value is False
        assert filtered.status == "sentinel"
        seen.clear()

        source.set(150)
        assert _kinds(seen) == ["DIRTY", "DATA"]
        assert filtered.cache() == 150
        seen.clear()

        source.set(70)
        assert _kinds(seen) == ["DIRTY", "RESOLVED"]
        assert filtered.cache() == 150
        assert filtered.status == "resolved"


def test_c12_downstream_recompute_from_resolved_emits_data_not_wedge():
    graph = Graph("py-c12-downstream-resolved")
    source = graph.state(100, name="source")
    filtered_seen: list[Message[object]] = []
    downstream_seen: list[Message[object]] = []

    def only_large(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            if int(value) >= 100:
                ctx.emit(value)

    filtered = graph.node([source], only_large, name="only-large")
    downstream = graph.derived([filtered], lambda value: int(value) * 2, name="double")

    with filtered.subscribe(filtered_seen.append), downstream.subscribe(downstream_seen.append):
        assert downstream.cache() == 200
        filtered_seen.clear()
        downstream_seen.clear()

        source.set(50)
        assert downstream.cache() == 200

    assert _kinds(filtered_seen) == ["DIRTY", "RESOLVED"]
    assert _kinds(downstream_seen) == ["DIRTY", "DATA"]


def test_c12_distinct_until_changed_is_opt_in_dedup():
    graph = Graph("py-c12-distinct-opt-in")
    source = graph.state(0, name="source")
    distinct_seen: list[Message[object]] = []

    def distinct(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            if not ctx.has_state or ctx.state != value:
                ctx.state = value
                ctx.emit(value)

    deduped = graph.node([source], distinct, name="distinct-until-changed")
    with deduped.subscribe(distinct_seen.append):
        distinct_seen.clear()

        source.set(1)
        source.set(1)
        source.set(2)
        source.set(2)
        source.set(3)

    assert _data_values(distinct_seen) == [1, 2, 3]
    assert _kinds(distinct_seen).count("RESOLVED") == 2


def test_c12_tier3_exclusivity_rejects_data_and_resolved_same_wave():
    graph = Graph("py-c12-tier3-exclusivity")
    stimulus = ConformanceStimulus(graph)
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    with source.subscribe(seen.append):
        seen.clear()
        stimulus.c12_dep_emits_data_resolved(source, 2)

    assert isinstance(seen[-1], ErrorMessage)
    assert "mix DATA and RESOLVED" in seen[-1].error.message
    assert source.status == "errored"


def test_c12_private_data_resolved_stimulus_rejects_awaitable_payload():
    graph = Graph("py-c12-awaitable-payload")
    stimulus = ConformanceStimulus(graph)
    source = graph.state(1, name="source")

    async def payload() -> int:
        return 2

    with pytest.raises(CallbackError, match="async callbacks are deferred"):
        stimulus.c12_dep_emits_data_resolved(source, payload())


def test_c11_public_rewire_next_subscribe_unsubscribe_replace_boundary():
    graph = Graph("py-c11-rewire-next")
    stimulus = ConformanceStimulus(graph)
    source = stimulus.state_empty("source")
    seen: list[Message[object]] = []
    activations: list[int] = []
    deactivations: list[int] = []
    queued_activation_snapshots: list[tuple[int, tuple[int, ...]]] = []
    current: list[object | None] = [None]

    def make_inner(value: int):
        def inner_body(ctx) -> None:
            activations.append(value)
            ctx.on_deactivation(lambda value=value: deactivations.append(value))
            ctx.emit(("inner", value))

        return graph.node([], inner_body, name=f"inner-{value}")

    def op_body(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            assert isinstance(value, int)
            inner = make_inner(value)
            queued_activation_snapshots.append((value, tuple(activations)))
            if value == 3:
                current[0] = inner
                ctx.rewire_next.replace_deps([source, inner], op_body)
            else:
                if current[0] is not None:
                    ctx.rewire_next.unsubscribe_dep(current[0], op_body)
                current[0] = inner
                ctx.rewire_next.subscribe_dep(inner, op_body)

        for index in range(1, ctx.dep_len):
            if ctx.terminal(index):
                if current[0] is not None:
                    ctx.rewire_next.unsubscribe_dep(current[0], op_body)
                continue
            for value in _fresh_values(ctx, index):
                ctx.emit(value)

    op = graph.node(
        [source],
        op_body,
        name="op",
        partial=True,
        complete_when_deps_complete=False,
        terminal_as_real_input=True,
    )

    with op.subscribe(seen.append):
        seen.clear()
        source.set(1)
        assert queued_activation_snapshots[-1] == (1, ())
        assert activations == [1]
        assert ("inner", 1) in _data_values(seen)

        seen.clear()
        source.set(2)
        assert deactivations == [1]
        assert activations == [1, 2]
        assert ("inner", 2) in _data_values(seen)

        seen.clear()
        source.set(3)
        assert deactivations == [1, 2]
        assert activations == [1, 2, 3]
        assert ("inner", 3) in _data_values(seen)

        seen.clear()
        assert current[0] is not None
        stimulus.c23_dep_completes(current[0])
        assert deactivations == [1, 2, 3]
        assert op.status != "completed"


def test_c11_terminal_owner_drains_queued_rewire_without_post_terminal_output():
    graph = Graph("py-c11-terminal-drain")
    stimulus = ConformanceStimulus(graph)
    source = stimulus.state_empty("source")
    seen: list[Message[object]] = []
    activations: list[str] = []

    def helper_body(ctx) -> None:
        activations.append("helper")
        ctx.emit("late-helper")

    helper = graph.node([], helper_body, name="helper")

    def terminal_body(ctx) -> None:
        if _fresh_values(ctx, 0):
            ctx.rewire_next.subscribe_dep(helper, terminal_body)
            conformance.down_complete(ctx)
        for index in range(1, ctx.dep_len):
            for value in _fresh_values(ctx, index):
                ctx.emit(value)

    op = graph.node([source], terminal_body, name="op", partial=True)
    with op.subscribe(seen.append):
        seen.clear()
        source.set(1)
        assert op.status == "completed"
        assert activations == ["helper"]
        assert not any(isinstance(message, DataMessage) for message in seen)

        seen.clear()
        stimulus.c17_dep_emits_data_then_completes(helper, "post-terminal")
        assert seen == []


def test_c11_terminal_owner_drains_unsubscribe_and_replace_variants():
    unsubscribe_graph = Graph("py-c11-terminal-unsubscribe")
    unsubscribe_stimulus = ConformanceStimulus(unsubscribe_graph)
    unsubscribe_source = unsubscribe_stimulus.state_empty("source")
    unsubscribe_seen: list[Message[object]] = []
    helper_activations: list[str] = []
    helper_deactivations: list[str] = []

    def helper_body(ctx) -> None:
        helper_activations.append("helper")
        ctx.on_deactivation(lambda: helper_deactivations.append("helper"))
        ctx.emit("helper")

    helper = unsubscribe_graph.node([], helper_body, name="helper")

    def unsubscribe_body(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            if value == "attach":
                ctx.rewire_next.subscribe_dep(helper, unsubscribe_body)
            elif value == "terminal-unsubscribe":
                ctx.rewire_next.unsubscribe_dep(helper, unsubscribe_body)
                conformance.down_complete(ctx)
        for index in range(1, ctx.dep_len):
            for value in _fresh_values(ctx, index):
                ctx.emit(value)

    unsubscribe_op = unsubscribe_graph.node(
        [unsubscribe_source],
        unsubscribe_body,
        name="unsubscribe-op",
        partial=True,
    )
    with unsubscribe_op.subscribe(unsubscribe_seen.append):
        unsubscribe_source.set("attach")
        assert helper_activations == ["helper"]

        unsubscribe_seen.clear()
        unsubscribe_source.set("terminal-unsubscribe")
        assert unsubscribe_op.status == "completed"
        assert helper_deactivations == ["helper"]
        assert not any(isinstance(message, DataMessage) for message in unsubscribe_seen)

        unsubscribe_seen.clear()
        unsubscribe_stimulus.c17_dep_emits_data_then_completes(helper, "post-terminal")
        assert unsubscribe_seen == []

    replace_graph = Graph("py-c11-terminal-replace")
    replace_stimulus = ConformanceStimulus(replace_graph)
    replace_source = replace_stimulus.state_empty("source")
    replace_seen: list[Message[object]] = []
    old_deactivations: list[str] = []
    new_activations: list[str] = []

    def old_body(ctx) -> None:
        ctx.on_deactivation(lambda: old_deactivations.append("old"))
        ctx.emit("old")

    def new_body(ctx) -> None:
        new_activations.append("new")
        ctx.emit("new")

    old_helper = replace_graph.node([], old_body, name="old-helper")
    new_helper = replace_graph.node([], new_body, name="new-helper")

    def replace_body(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            if value == "attach":
                ctx.rewire_next.subscribe_dep(old_helper, replace_body)
            elif value == "terminal-replace":
                ctx.rewire_next.replace_deps([replace_source, new_helper], replace_body)
                conformance.down_complete(ctx)
        for index in range(1, ctx.dep_len):
            for value in _fresh_values(ctx, index):
                ctx.emit(value)

    replace_op = replace_graph.node([replace_source], replace_body, name="replace-op", partial=True)
    with replace_op.subscribe(replace_seen.append):
        replace_source.set("attach")

        replace_seen.clear()
        replace_source.set("terminal-replace")
        assert replace_op.status == "completed"
        assert old_deactivations == ["old"]
        assert new_activations == ["new"]
        assert not any(isinstance(message, DataMessage) for message in replace_seen)

        replace_seen.clear()
        replace_stimulus.c17_dep_emits_data_then_completes(new_helper, "post-terminal")
        assert replace_seen == []


def test_c11_immediate_in_fn_self_rewire_is_d37_error_private_harness():
    graph = Graph("py-c11-immediate-reject")
    stimulus = ConformanceStimulus(graph)
    source = stimulus.state_empty("source")
    helper = graph.node([], lambda ctx: ctx.emit("helper"), name="helper")
    seen: list[Message[object]] = []
    bad_holder: list[object] = []

    def bad_body(ctx) -> None:
        if _fresh_values(ctx, 0):
            stimulus.c11_immediate_subscribe_dep(bad_holder[0], helper, bad_body)

    bad = graph.node([source], bad_body, name="bad", partial=True)
    bad_holder.append(bad)

    with bad.subscribe(seen.append):
        seen.clear()
        source.set(1)
        errors = [message for message in seen if isinstance(message, ErrorMessage)]
        assert bad.status == "errored"
        assert errors
        assert "D37" in errors[-1].error.message


def test_c11_no_net_change_rewire_next_is_noop():
    graph = Graph("py-c11-no-net-change")
    stimulus = ConformanceStimulus(graph)
    source = stimulus.state_empty("source")
    seen: list[Message[object]] = []
    runs = 0

    def body(ctx) -> None:
        nonlocal runs
        for value in _fresh_values(ctx, 0):
            runs += 1
            ctx.rewire_next.replace_deps([source], body)
            ctx.emit(value)

    node = graph.node([source], body, name="node", partial=True)
    with node.subscribe(seen.append):
        seen.clear()
        source.set(1)
        assert runs == 1
        assert _data_values(seen) == [1]


def test_c11_public_rewire_next_rejects_non_callable_callback():
    graph = Graph("py-c11-callback-validation")
    stimulus = ConformanceStimulus(graph)
    source = stimulus.state_empty("source")
    helper = graph.state("helper", name="helper")

    def body(ctx) -> None:
        if _fresh_values(ctx, 0):
            with pytest.raises(GraphReflyValueError, match="callback must be callable"):
                ctx.rewire_next.subscribe_dep(helper, object())  # type: ignore[arg-type]

    node = graph.node([source], body, name="node", partial=True)
    with node.subscribe(lambda _message: None):
        source.set(1)

    assert node.status != "errored"


def test_c11_public_rewire_next_merge_keeps_multiple_inners_live():
    graph = Graph("py-c11-merge")
    stimulus = ConformanceStimulus(graph)
    source = stimulus.state_empty("source")
    inners: dict[str, object] = {}
    seen: list[Message[object]] = []

    def op_body(ctx) -> None:
        for label in _fresh_values(ctx, 0):
            assert isinstance(label, str)
            inner = graph.state(("cached", label), name=f"inner-{label}")
            inners[label] = inner
            ctx.rewire_next.subscribe_dep(inner, op_body)
        for index in range(1, ctx.dep_len):
            for value in _fresh_values(ctx, index):
                ctx.emit(value)

    op = graph.node([source], op_body, name="op", partial=True)
    with op.subscribe(seen.append):
        source.set("A")
        source.set("B")
        seen.clear()

        inner_a = inners["A"]
        inner_b = inners["B"]
        assert hasattr(inner_a, "set")
        assert hasattr(inner_b, "set")
        inner_a.set(("live", "A"))
        inner_b.set(("live", "B"))

    assert ("live", "A") in _data_values(seen)
    assert ("live", "B") in _data_values(seen)


def test_c25_public_rewire_next_batch_commit_old_shape_before_drain():
    graph = Graph("py-c25-batch-commit")
    source = graph.state("idle", name="source")
    helper_activations: list[str] = []
    seen: list[Message[object]] = []

    def helper_body(ctx) -> None:
        helper_activations.append("helper")
        ctx.emit("helper")

    helper = graph.node([], helper_body, name="helper")

    def op_body(ctx) -> None:
        if _fresh_values(ctx, 0):
            label = _fresh_values(ctx, 0)[-1]
            if label == "attach":
                ctx.rewire_next.subscribe_dep(helper, op_body)
                ctx.emit("old-shape")
        for index in range(1, ctx.dep_len):
            for value in _fresh_values(ctx, index):
                ctx.emit(value)

    op = graph.node([source], op_body, name="op", partial=True)
    with op.subscribe(seen.append):
        seen.clear()

        def body() -> None:
            source.set("attach")
            assert helper_activations == []

        graph.batch(body)

    values = _data_values(seen)
    assert values == ["old-shape", "helper"]
    assert helper_activations == ["helper"]


def test_c25_public_rewire_next_batch_rollback_drops_tasks():
    graph = Graph("py-c25-rollback")
    subscribe_source = graph.state("attach-rollback", name="subscribe-source")
    rollback_helper = graph.state("rollback-helper", name="rollback-helper")
    subscribe_seen: list[Message[object]] = []
    subscribe_queued: list[str] = []
    subscribe_holders: list[object] = []

    def subscribe_body(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            if value == "attach-rollback":
                subscribe_queued.append("subscribe-rollback")
                ctx.rewire_next.subscribe_dep(rollback_helper, subscribe_body)
        for index in range(1, ctx.dep_len):
            for value in _fresh_values(ctx, index):
                ctx.emit(value)

    subscribe_op = graph.node([subscribe_source], subscribe_body, name="subscribe-op", partial=True)

    def rollback_subscribe() -> None:
        subscribe_holders.append(subscribe_op.subscribe(subscribe_seen.append))
        assert subscribe_queued == ["subscribe-rollback"]
        raise ValueError("rollback")

    with pytest.raises(ValueError, match="rollback"):
        graph.batch(rollback_subscribe)
    assert subscribe_queued == ["subscribe-rollback"]
    rollback_helper.set("must-not-drive-after-subscribe-rollback")
    assert "must-not-drive-after-subscribe-rollback" not in _data_values(subscribe_seen)
    subscribe_holders.pop().unsubscribe()

    drop_source = graph.state("drop", name="drop-source")
    helper = graph.state("helper-1", name="helper")
    replacement = graph.state("replacement", name="replacement")
    drop_seen: list[Message[object]] = []
    drop_queued: list[str] = []
    drop_holders: list[object] = []

    def drop_body(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            if value == "drop":
                drop_queued.append("drop-rollback")
                ctx.rewire_next.unsubscribe_dep(helper, drop_body)
                ctx.rewire_next.replace_deps([drop_source, replacement], drop_body)
        for index in range(1, ctx.dep_len):
            for value in _fresh_values(ctx, index):
                ctx.emit(value)

    drop_op = graph.node([drop_source, helper], drop_body, name="drop-op", partial=True)

    def rollback_drop() -> None:
        drop_holders.append(drop_op.subscribe(drop_seen.append))
        assert drop_queued == ["drop-rollback"]
        raise ValueError("rollback")

    with pytest.raises(ValueError, match="rollback"):
        graph.batch(rollback_drop)
    assert drop_queued == ["drop-rollback"]

    drop_seen.clear()
    helper.set("still-live")
    replacement.set("must-not-drive")
    drop_holders.pop().unsubscribe()

    assert "still-live" in _data_values(drop_seen)
    assert "must-not-drive" not in _data_values(drop_seen)


def test_c25_public_rewire_next_paused_batch_rollback_drops_task_before_resume():
    graph = Graph("py-c25-rewire-paused-rollback")
    source = graph.state("queue", name="source")
    helper_activations: list[str] = []
    op_holder: list[object] = []
    subscriptions: list[object] = []
    queued: list[str] = []

    def helper_body(ctx) -> None:
        helper_activations.append("helper")
        ctx.emit("helper")

    helper = graph.node([], helper_body, name="helper")

    def op_body(ctx) -> None:
        if "queue" in _fresh_values(ctx, 0):
            queued.append("rewire")
            ctx.rewire_next.subscribe_dep(helper, op_body)
            graph.pause(op_holder[0], "rollback")

    op = graph.node([source], op_body, name="op", partial=True)
    op_holder.append(op)

    def body() -> None:
        subscriptions.append(op.subscribe(lambda _message: None))
        assert queued == ["rewire"]
        raise ValueError("rollback")

    with pytest.raises(ValueError, match="rollback"):
        graph.batch(body)

    assert queued == ["rewire"]
    assert helper_activations == []
    graph.resume(op, "rollback")
    assert queued == ["rewire"]
    assert helper_activations == []
    subscriptions.pop().unsubscribe()


def test_c25_public_rewire_next_pause_final_lock_gating():
    graph = Graph("py-c25-pause")
    source = graph.state("idle", name="source")
    helper_activations: list[str] = []
    op_holder: list[object] = []

    def helper_body(ctx) -> None:
        helper_activations.append("helper")
        ctx.emit("helper")

    helper = graph.node([], helper_body, name="helper")

    def op_body(ctx) -> None:
        if "queue" in _fresh_values(ctx, 0):
            ctx.rewire_next.subscribe_dep(helper, op_body)
            graph.pause(op_holder[0], "A")
            graph.pause(op_holder[0], "B")

    op = graph.node([source], op_body, name="op", partial=True)
    op_holder.append(op)

    with op.subscribe(lambda _message: None):
        source.set("queue")
        assert helper_activations == []
        graph.resume(op, "A")
        assert helper_activations == []
        graph.resume(op, "B")
        assert helper_activations == ["helper"]


def test_c25_public_rewire_next_combined_batch_pause_ordering():
    graph = Graph("py-c25-batch-pause")
    source = graph.state("idle", name="source")
    helper_activations: list[int] = []
    op_holder: list[object] = []

    def make_helper(value: int):
        def helper_body(ctx) -> None:
            helper_activations.append(value)
            ctx.emit(("helper", value))

        return graph.node([], helper_body, name=f"helper-{value}")

    def op_body(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            if not isinstance(value, int):
                continue
            helper = make_helper(value)
            ctx.rewire_next.subscribe_dep(helper, op_body)
            graph.pause(op_holder[0], f"lock-{value}")

    op = graph.node([source], op_body, name="op", partial=True)
    op_holder.append(op)

    with op.subscribe(lambda _message: None):
        graph.batch(lambda: source.set(1))
        assert helper_activations == []
        graph.resume(op, "lock-1")
        assert helper_activations == [1]


def test_c25_public_rewire_next_final_resume_inside_open_batch_ordering():
    graph = Graph("py-c25-rewire-resume-inside-batch")
    source = graph.state("idle", name="source")
    events: list[str] = []
    helper_activations: list[str] = []

    def helper_body(ctx) -> None:
        helper_activations.append("helper")
        events.append("helper")
        ctx.emit("helper")

    helper = graph.node([], helper_body, name="helper")

    def op_body(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            if value != "inside":
                continue
            events.append(f"op:{value}")
            ctx.rewire_next.subscribe_dep(helper, op_body)
            ctx.emit(f"old:{value}")
        for index in range(1, ctx.dep_len):
            for value in _fresh_values(ctx, index):
                ctx.emit(value)

    op = graph.node([source], op_body, name="op", partial=True)
    seen: list[Message[object]] = []
    with op.subscribe(seen.append):
        seen.clear()
        graph.pause(op, "gate")

        def body() -> None:
            source.set("inside")
            assert events == []
            graph.resume(op, "gate")
            assert events == []
            assert helper_activations == []
            assert _data_values(seen) == []

        graph.batch(body)

    assert events == ["op:inside", "helper"]
    assert helper_activations == ["helper"]
    assert _data_values(seen) == ["old:inside", "helper"]


def test_c25_public_request_pull_next_batch_commit_old_shape_before_drain():
    graph = Graph("py-c25-pull-batch-commit")
    acc = graph.state(0, name="acc")
    trigger = graph.state("idle", name="trigger")
    events: list[str] = []
    pull_params: list[object | None] = []
    seen: list[Message[object]] = []

    def snapshot(ctx) -> None:
        events.append("snap")
        pull_params.append(ctx.pull_params())
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    snap = graph.node([acc], snapshot, name="snap", pull_id="snapshot")

    def demand(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            if value != "commit":
                continue
            events.append(f"demand:{value}")
            ctx.request_pull_next("snapshot", {"batch": value}, toward_dep=1)
            ctx.emit(f"old:{value}")
        for value in _fresh_values(ctx, 1):
            ctx.emit(("snap", value))

    op = graph.node([trigger, snap], demand, name="op", partial=True)
    with op.subscribe(seen.append):
        seen.clear()

        def body() -> None:
            acc.set(1)
            trigger.set("commit")
            assert events == []
            assert pull_params == []

        graph.batch(body)

    assert events == ["demand:commit", "snap"]
    assert pull_params == [{"batch": "commit"}]
    assert _data_values(seen) == ["old:commit", ("snap", 1)]


def test_c25_public_request_pull_next_batch_rollback_drops_task():
    graph = Graph("py-c25-pull-rollback")
    acc = graph.state(0, name="acc")
    trigger = graph.state("rollback", name="trigger")
    pull_params: list[object | None] = []
    queued: list[str] = []
    subscriptions: list[object] = []

    def snapshot(ctx) -> None:
        pull_params.append(ctx.pull_params())
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    snap = graph.node([acc], snapshot, name="snap", pull_id="snapshot")

    def demand(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            if value not in {"rollback", "commit"}:
                continue
            queued.append(value)
            ctx.request_pull_next("snapshot", value, toward_dep=1)
        for value in _fresh_values(ctx, 1):
            ctx.emit(value)

    op = graph.node([trigger, snap], demand, name="op", partial=True)
    def rollback_pull() -> None:
        subscriptions.append(op.subscribe(lambda _message: None))
        assert queued == ["rollback"]
        raise ValueError("rollback")

    with pytest.raises(ValueError, match="rollback"):
        graph.batch(rollback_pull)
    assert queued == ["rollback"]
    assert pull_params == []

    trigger.set("commit")
    subscriptions.pop().unsubscribe()

    assert queued == ["rollback", "commit"]
    assert pull_params == ["commit"]


def test_c25_public_request_pull_next_paused_batch_rollback_drops_task_before_resume():
    graph = Graph("py-c25-pull-paused-rollback")
    acc = graph.state(1, name="acc")
    trigger = graph.state("queue", name="trigger")
    pull_params: list[object | None] = []
    op_holder: list[object] = []
    subscriptions: list[object] = []
    queued: list[str] = []

    def snapshot(ctx) -> None:
        pull_params.append(ctx.pull_params())
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    snap = graph.node([acc], snapshot, name="snap", pull_id="snapshot")

    def demand(ctx) -> None:
        if "queue" in _fresh_values(ctx, 0):
            queued.append("pull")
            ctx.request_pull_next("snapshot", "rollback", toward_dep=1)
            graph.pause(op_holder[0], "rollback")
        for value in _fresh_values(ctx, 1):
            ctx.emit(value)

    op = graph.node([trigger, snap], demand, name="op", partial=True)
    op_holder.append(op)

    def body() -> None:
        subscriptions.append(op.subscribe(lambda _message: None))
        assert queued == ["pull"]
        raise ValueError("rollback")

    with pytest.raises(ValueError, match="rollback"):
        graph.batch(body)

    assert queued == ["pull"]
    assert pull_params == []
    graph.resume(op, "rollback")
    assert queued == ["pull"]
    assert pull_params == []
    subscriptions.pop().unsubscribe()


def test_c25_public_request_pull_next_pause_final_lock_gating():
    graph = Graph("py-c25-pull-pause")
    acc = graph.state(1, name="acc")
    trigger = graph.state("idle", name="trigger")
    pull_params: list[object | None] = []
    op_holder: list[object] = []

    def snapshot(ctx) -> None:
        pull_params.append(ctx.pull_params())
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    snap = graph.node([acc], snapshot, name="snap", pull_id="snapshot")

    def demand(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            if value != "pause":
                continue
            ctx.request_pull_next("snapshot", value, toward_dep=1)
            graph.pause(op_holder[0], "A")
            graph.pause(op_holder[0], "B")
        for value in _fresh_values(ctx, 1):
            ctx.emit(value)

    op = graph.node([trigger, snap], demand, name="op", partial=True)
    op_holder.append(op)

    with op.subscribe(lambda _message: None):
        trigger.set("pause")
        assert pull_params == []
        graph.resume(op, "A")
        assert pull_params == []
        graph.resume(op, "B")
        assert pull_params == ["pause"]


def test_c25_public_request_pull_next_combined_commit_before_final_resume_ordering():
    graph = Graph("py-c25-pull-batch-then-resume")
    acc = graph.state(1, name="acc")
    trigger = graph.state("idle", name="trigger")
    pull_params: list[object | None] = []
    op_holder: list[object] = []

    def snapshot(ctx) -> None:
        pull_params.append(ctx.pull_params())
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    snap = graph.node([acc], snapshot, name="snap", pull_id="snapshot")

    def demand(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            if not isinstance(value, int):
                continue
            ctx.request_pull_next("snapshot", value, toward_dep=1)
            graph.pause(op_holder[0], f"lock-{value}")
        for value in _fresh_values(ctx, 1):
            ctx.emit(value)

    op = graph.node([trigger, snap], demand, name="op", partial=True)
    op_holder.append(op)

    with op.subscribe(lambda _message: None):
        graph.batch(lambda: trigger.set(1))
        assert pull_params == []
        graph.resume(op, "lock-1")
        assert pull_params == [1]


def test_c25_public_request_pull_next_final_resume_inside_open_batch_ordering():
    graph = Graph("py-c25-pull-resume-inside-batch")
    acc = graph.state(1, name="acc")
    trigger = graph.state("idle", name="trigger")
    events: list[str] = []
    pull_params: list[object | None] = []
    seen: list[Message[object]] = []

    def snapshot(ctx) -> None:
        events.append("snap")
        pull_params.append(ctx.pull_params())
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    snap = graph.node([acc], snapshot, name="snap", pull_id="snapshot")

    def demand(ctx) -> None:
        for value in _fresh_values(ctx, 0):
            if value != "batch":
                continue
            events.append(f"demand:{value}")
            ctx.request_pull_next("snapshot", {"inside": value}, toward_dep=1)
            ctx.emit(f"old:{value}")
        for value in _fresh_values(ctx, 1):
            ctx.emit(("snap", value))

    op = graph.node([trigger, snap], demand, name="op", partial=True)
    with op.subscribe(seen.append):
        seen.clear()
        graph.pause(op, "gate")

        def body() -> None:
            trigger.set("batch")
            assert events == []
            graph.resume(op, "gate")
            assert events == []
            assert pull_params == []
            assert _data_values(seen) == []

        graph.batch(body)

    assert events == ["demand:batch", "snap"]
    assert pull_params == [{"inside": "batch"}]
    assert _data_values(seen) == ["old:batch", ("snap", 1)]


def test_c15_dep_complete_releases_dirty_and_joins_once():
    graph = Graph("py-c15-complete-mid-dirty")
    stimulus = ConformanceStimulus(graph)
    b = graph.state(1, name="b")
    c = graph.state(10, name="c")
    runs = 0
    seen: list[Message[object]] = []

    def sum2(ctx) -> None:
        nonlocal runs
        runs += 1
        left = ctx.data(0, 0)
        right = ctx.data(1, 0)
        ctx.emit(left + right)

    d = graph.node(
        [b, c],
        sum2,
        name="d",
        complete_when_deps_complete=False,
    )

    with d.subscribe(seen.append):
        assert d.cache() == 11
        runs = 0
        seen.clear()

        stimulus.c15_dep_goes_dirty(b)
        assert d.status == "dirty"
        c.set(20)
        assert runs == 0
        assert d.cache() == 11
        stimulus.c15_dirty_dep_completes_without_data(b)

        assert _kinds(seen) == ["DIRTY", "DATA"]
        assert runs == 1
        assert d.cache() == 21
        assert d.status != "completed"


def test_c15_dep_complete_sole_dirty_contributor_un_dirties_without_data():
    graph = Graph("py-c15-sole-dirty-complete")
    stimulus = ConformanceStimulus(graph)
    b = graph.state(1, name="b")
    c = graph.state(10, name="c")
    seen: list[Message[object]] = []

    def sum2(ctx) -> None:
        ctx.emit(ctx.data(0, 0) + ctx.data(1, 0))

    d = graph.node(
        [b, c],
        sum2,
        name="d",
        complete_when_deps_complete=False,
    )

    with d.subscribe(seen.append):
        assert d.cache() == 11
        seen.clear()

        stimulus.c15_dep_goes_dirty(b)
        stimulus.c15_dirty_dep_completes_without_data(b)

        assert _kinds(seen) == ["DIRTY", "RESOLVED"]
        assert d.cache() == 11
        assert d.status == "resolved"
        assert d.status != "completed"


def test_c15_absorbed_error_releases_dirty_and_can_read_terminal():
    graph = Graph("py-c15-rescue-error")
    stimulus = ConformanceStimulus(graph)
    b = graph.state(1, name="b")
    c = graph.state(10, name="c")
    seen: list[Message[object]] = []

    def rescue(ctx) -> None:
        left = 0 if ctx.terminal(0) == "boom" else ctx.data(0, 0)
        right = ctx.data(1, 0)
        ctx.emit(left + right)

    d = graph.node(
        [b, c],
        rescue,
        name="d",
        error_when_deps_error=False,
        terminal_as_real_input=True,
    )

    with d.subscribe(seen.append):
        assert d.cache() == 11
        seen.clear()

        stimulus.c15_dep_goes_dirty(b)
        stimulus.c15_dirty_dep_errors_without_data(b, "boom")

        assert _kinds(seen) == ["DIRTY", "DATA"]
        assert not any(isinstance(message, ErrorMessage) for message in seen)
        assert d.status != "errored"
        assert d.status != "completed"
        assert d.cache() == 10


def test_c15_gate_holds_terminal_dirty_release_un_dirties_without_running():
    graph = Graph("py-c15-gate-holds")
    stimulus = ConformanceStimulus(graph)
    b = graph.state(0, name="b")
    c = graph.node([], lambda _ctx: None, name="empty-c")
    runs = 0
    seen: list[Message[object]] = []

    def sum2(ctx) -> None:
        nonlocal runs
        runs += 1
        ctx.emit(ctx.data(0, 0) + ctx.data(1, 0))

    d = graph.node(
        [b, c],
        sum2,
        name="d",
        complete_when_deps_complete=False,
    )

    with d.subscribe(seen.append):
        assert runs == 0
        assert d.has_value is False
        seen.clear()

        stimulus.c15_dep_goes_dirty(c)
        b.set(5)
        assert runs == 0
        stimulus.c15_dirty_dep_completes_without_data(c)

        assert runs == 0
        assert _kinds(seen) == ["DIRTY", "RESOLVED"]
        assert d.has_value is False


def test_c17_absorbed_error_then_complete_auto_completes():
    graph = Graph("py-c17-error-then-complete")
    stimulus = ConformanceStimulus(graph)
    b = graph.node([], lambda _ctx: None, name="empty-b")
    c = graph.node([], lambda _ctx: None, name="empty-c")
    seen: list[Message[object]] = []

    def forward_b(ctx) -> None:
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    d = graph.node(
        [b, c],
        forward_b,
        name="d",
        error_when_deps_error=False,
    )

    with d.subscribe(seen.append):
        seen.clear()
        stimulus.c17_dep_errors(c, "boom")
        assert d.status != "completed"
        assert d.status != "errored"

        stimulus.c17_dep_emits_data_then_completes(b, 1)

        assert _kinds(seen).count("COMPLETE") == 1
        assert d.status == "completed"


def test_c17_complete_then_absorbed_error_auto_completes_order_independent():
    graph = Graph("py-c17-complete-then-error")
    stimulus = ConformanceStimulus(graph)
    b = graph.node([], lambda _ctx: None, name="empty-b")
    c = graph.node([], lambda _ctx: None, name="empty-c")
    seen: list[Message[object]] = []

    def forward_b(ctx) -> None:
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    d = graph.node(
        [b, c],
        forward_b,
        name="d",
        error_when_deps_error=False,
    )

    with d.subscribe(seen.append):
        seen.clear()
        stimulus.c17_dep_emits_data_then_completes(b, 1)
        assert _kinds(seen).count("COMPLETE") == 0
        assert d.status != "completed"
        assert d.status != "errored"

        stimulus.c17_dep_errors(c, "boom")

        assert _kinds(seen).count("COMPLETE") == 1
        assert d.status == "completed"


def test_c17_default_error_cascade_does_not_take_absorbed_path():
    graph = Graph("py-c17-default-error-cascade")
    stimulus = ConformanceStimulus(graph)
    b = graph.node([], lambda _ctx: None, name="empty-b")
    c = graph.node([], lambda _ctx: None, name="empty-c")
    seen: list[Message[object]] = []

    def forward_b(ctx) -> None:
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    d = graph.node([b, c], forward_b, name="d")

    with d.subscribe(seen.append):
        seen.clear()
        stimulus.c17_dep_errors(c, "boom")

        assert any(isinstance(message, ErrorMessage) for message in seen)
        assert ControlMessage("COMPLETE") not in seen
        assert d.status == "errored"


def test_c19_undirty_resolved_timing_respects_resumeall_and_batch():
    def sum2(ctx) -> None:
        ctx.emit(ctx.data(0, 0) + ctx.data(1, 0))

    resume_graph = Graph("py-c19-resumeall-terminal")
    resume_stimulus = ConformanceStimulus(resume_graph)
    rb = resume_graph.state(1, name="b")
    rc = resume_graph.state(10, name="c")
    rseen: list[Message[object]] = []
    rd = resume_stimulus.node(
        [rb, rc],
        sum2,
        name="d",
        pausable="resumeAll",
        complete_when_deps_complete=False,
    )
    with rd.subscribe(rseen.append):
        assert rd.cache() == 11
        rseen.clear()

        resume_graph.pause(rd, "resumeall")
        resume_stimulus.c15_dep_goes_dirty(rb)
        resume_stimulus.c15_dirty_dep_completes_without_data(rb)

        assert _kinds(rseen) == ["DIRTY"]
        resume_graph.resume(rd, "resumeall")
        assert _kinds(rseen) == ["DIRTY", "RESOLVED"]
        assert rd.cache() == 11

    batch_graph = Graph("py-c19-batch-terminal")
    batch_stimulus = ConformanceStimulus(batch_graph)
    bb = batch_graph.state(1, name="b")
    bc = batch_graph.state(10, name="c")
    bseen: list[Message[object]] = []
    bd = batch_graph.node(
        [bb, bc],
        sum2,
        name="d",
        complete_when_deps_complete=False,
    )
    with bd.subscribe(bseen.append):
        assert bd.cache() == 11
        bseen.clear()

        def batch_body() -> None:
            batch_stimulus.c15_dep_goes_dirty(bb)
            batch_stimulus.c15_dirty_dep_completes_without_data(bb)
            assert _kinds(bseen) == ["DIRTY"]

        batch_graph.batch(batch_body)
        assert _kinds(bseen) == ["DIRTY", "RESOLVED"]

    invalidate_resume_graph = Graph("py-c19-resumeall-invalidate")
    invalidate_resume_stimulus = ConformanceStimulus(invalidate_resume_graph)
    irb = invalidate_resume_graph.state(1, name="b")
    irc = invalidate_resume_stimulus.state_empty("c")
    irseen: list[Message[object]] = []
    ird = invalidate_resume_stimulus.node(
        [irb, irc],
        sum2,
        name="d",
        pausable="resumeAll",
    )
    with ird.subscribe(irseen.append):
        assert ird.has_value is False
        irseen.clear()

        invalidate_resume_graph.pause(ird, "resumeall-invalidate")
        invalidate_resume_stimulus.c15_dep_goes_dirty(irb)
        invalidate_resume_stimulus.c19_dep_invalidates_after_dirty(irb)

        assert _kinds(irseen) == ["DIRTY"]
        invalidate_resume_graph.resume(ird, "resumeall-invalidate")
        assert _kinds(irseen) == ["DIRTY", "RESOLVED"]
        assert ird.has_value is False

    invalidate_batch_graph = Graph("py-c19-batch-invalidate")
    invalidate_batch_stimulus = ConformanceStimulus(invalidate_batch_graph)
    ibb = invalidate_batch_graph.state(1, name="b")
    ibc = invalidate_batch_stimulus.state_empty("c")
    ibseen: list[Message[object]] = []
    ibd = invalidate_batch_graph.node([ibb, ibc], sum2, name="d")
    with ibd.subscribe(ibseen.append):
        assert ibd.has_value is False
        ibseen.clear()

        def invalidate_batch_body() -> None:
            invalidate_batch_stimulus.c15_dep_goes_dirty(ibb)
            invalidate_batch_stimulus.c19_dep_invalidates_after_dirty(ibb)
            assert _kinds(ibseen) == ["DIRTY"]

        invalidate_batch_graph.batch(invalidate_batch_body)
        assert _kinds(ibseen) == ["DIRTY", "RESOLVED"]
        assert ibd.has_value is False

    default_graph = Graph("py-c19-default-terminal")
    default_stimulus = ConformanceStimulus(default_graph)
    db = default_graph.state(1, name="b")
    dc = default_graph.state(10, name="c")
    dseen: list[Message[object]] = []
    dd = default_graph.node(
        [db, dc],
        sum2,
        name="d",
        complete_when_deps_complete=False,
    )
    with dd.subscribe(dseen.append):
        assert dd.cache() == 11
        dseen.clear()
        default_graph.pause(dd, "default")
        default_stimulus.c15_dep_goes_dirty(db)
        default_stimulus.c15_dirty_dep_completes_without_data(db)
        assert _kinds(dseen) == ["DIRTY", "RESOLVED"]


def test_c20_teardown_relays_through_terminal_intermediate_without_resurrection():
    graph = Graph("py-c20-terminal-teardown")
    stimulus = ConformanceStimulus(graph)
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    def forward(ctx) -> None:
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    mid = graph.node(
        [source],
        forward,
        name="mid",
        complete_when_deps_complete=False,
    )
    with mid.subscribe(seen.append):
        assert mid.cache() == 1
        stimulus.c23_dep_completes(mid)
        assert mid.status == "completed"
        seen.clear()

        source.set(2)
        assert seen == []
        assert mid.status == "completed"

        stimulus.c20_dep_tears_down(source)
        assert _kinds(seen) == ["TEARDOWN"]
        assert mid.status == "completed"

    live_graph = Graph("py-c20-live-teardown")
    live_stimulus = ConformanceStimulus(live_graph)
    live_source = live_graph.state(1, name="source")
    live_seen: list[Message[object]] = []
    live_mid = live_graph.node([live_source], forward, name="mid")
    with live_mid.subscribe(live_seen.append):
        assert live_mid.cache() == 1
        live_seen.clear()
        live_stimulus.c20_dep_tears_down(live_source)
        assert _kinds(live_seen) == ["COMPLETE", "TEARDOWN"]
        assert live_mid.status == "completed"


def test_c16_pull_family_public_quiet_self_demand_routing_and_resumeall():
    graph = Graph("py-c16-pull-quiet")
    stimulus = ConformanceStimulus(graph)
    acc = graph.state(0, name="acc")
    delta = stimulus.state_empty("delta")
    seen: list[Message[object]] = []
    pull_seen: list[object | None] = []

    def snapshot(ctx) -> None:
        assert isinstance(ctx.pull, PullContext)
        pull_seen.append(ctx.pull_params())
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    snap = graph.node([acc], snapshot, name="snap", pull_id="snapshot")

    def consumer(ctx) -> None:
        trigger_values = _fresh_values(ctx, 0)
        if trigger_values:
            ctx.request_pull_next(
                "snapshot",
                {"limit": trigger_values[-1]},
                toward_dep=1,
            )
        snap_values = _fresh_values(ctx, 1)
        if snap_values:
            ctx.emit(snap_values[-1])

    demand = graph.node([delta, snap], consumer, name="demand", partial=True)
    with demand.subscribe(seen.append):
        seen.clear()
        acc.set(1)
        acc.set(2)
        assert seen == []

        delta.set(1)
        assert pull_seen[-1] == {"limit": 1}
        assert _kinds(seen)[-2:] == ["DIRTY", "DATA"]
        assert demand.cache() == 2
        seen.clear()

        graph.resume(snap, "snapshot")
        assert seen == []

    with pytest.raises(GraphReflyValueError, match="pull_id nodes cannot use pausable=False"):
        graph.node([acc], snapshot, pull_id="bad", pausable=False)

    routed_graph = Graph("py-c16-routed-sibling")
    routed_stimulus = ConformanceStimulus(routed_graph)
    routed_acc = routed_graph.state(0, name="acc")
    routed_trigger = routed_stimulus.state_empty("trigger")
    f_runs = 0
    h_runs = 0

    def f_snap(ctx) -> None:
        nonlocal f_runs
        f_runs += 1
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    def h_snap(ctx) -> None:
        nonlocal h_runs
        h_runs += 1
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    f = routed_graph.node([routed_acc], f_snap, name="f", pull_id="F")
    h = routed_graph.node([routed_acc], h_snap, name="h", pull_id="H")
    g = routed_graph.node([f, h], lambda ctx: None, name="g", partial=True)

    def d(ctx) -> None:
        if _fresh_values(ctx, 0):
            ctx.request_pull_next("F")

    d_node = routed_graph.node([routed_trigger, g], d, name="d", partial=True)
    with d_node.subscribe(lambda _msg: None):
        routed_acc.set(1)
        routed_trigger.set(1)
        assert f_runs == 1
        assert h_runs == 0

    backlog_graph = Graph("py-c16-resumeall-backlog")
    backlog_stimulus = ConformanceStimulus(backlog_graph)
    backlog_acc = backlog_graph.state(0, name="acc")
    backlog_trigger = backlog_stimulus.state_empty("trigger")
    backlog_values: list[int] = []

    backlog_snap = backlog_graph.node(
        [backlog_acc],
        lambda ctx: ctx.emit(ctx.data(0)) if ctx.has_data(0) else None,
        name="snap",
        pull_id="snapshot",
        pausable="resumeAll",
    )

    def backlog_consumer(ctx) -> None:
        if _fresh_values(ctx, 0):
            ctx.request_pull_next("snapshot", toward_dep=1)
        snap_values = _fresh_values(ctx, 1)
        if snap_values:
            value = snap_values[-1]
            assert isinstance(value, int)
            backlog_values.append(value)
            ctx.emit(value)

    backlog_demand = backlog_graph.node(
        [backlog_trigger, backlog_snap],
        backlog_consumer,
        name="demand",
        partial=True,
    )
    with backlog_demand.subscribe(lambda _msg: None):
        backlog_acc.set(1)
        backlog_acc.set(2)
        assert backlog_values == []
        backlog_trigger.set(1)
        assert backlog_values == [0, 1, 2]

    immediate_graph = Graph("py-c16-immediate-pull-d37")
    immediate_stimulus = ConformanceStimulus(immediate_graph)
    immediate_acc = immediate_graph.state(1, name="acc")
    immediate_trigger = immediate_stimulus.state_empty("trigger")
    immediate_seen: list[Message[object]] = []
    immediate_snap = immediate_graph.node(
        [immediate_acc],
        lambda ctx: ctx.emit(ctx.data(0)) if ctx.has_data(0) else None,
        name="snap",
        pull_id="snapshot",
    )

    def immediate_requester(ctx) -> None:
        if _fresh_values(ctx, 0):
            ctx.request_pull("snapshot", toward_dep=1)

    immediate_bad = immediate_graph.node(
        [immediate_trigger, immediate_snap],
        immediate_requester,
        name="bad",
        partial=True,
    )
    with immediate_bad.subscribe(immediate_seen.append):
        immediate_trigger.set(1)
        errors = [msg for msg in immediate_seen if isinstance(msg, ErrorMessage)]
        assert immediate_bad.status == "errored"
        assert "D37" in errors[-1].error.message


def test_c18_pull_family_public_routed_demand_over_diamond_fires_holder_once():
    graph = Graph("py-c18-pull-diamond")
    stimulus = ConformanceStimulus(graph)
    acc = graph.state(0, name="acc")
    trigger = stimulus.state_empty("trigger")
    snap_runs = 0
    seen_params: list[object | None] = []

    def snapshot(ctx) -> None:
        nonlocal snap_runs
        snap_runs += 1
        seen_params.append(ctx.pull_params())
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    def forward(ctx) -> None:
        for dep_waves in ctx.wave_data:
            for wave in dep_waves:
                for value in wave:
                    if value is not SENTINEL:
                        ctx.emit(value)

    snap = graph.node([acc], snapshot, name="snap", pull_id="snapshot")
    g1 = graph.node([snap], forward, name="g1", partial=True)
    g2 = graph.node([snap], forward, name="g2", partial=True)

    def d(ctx) -> None:
        if _fresh_values(ctx, 0):
            ctx.request_pull_next("snapshot", {"via": "diamond"})

    demand = graph.node([trigger, g1, g2], d, name="d", partial=True)
    with demand.subscribe(lambda _msg: None):
        acc.set(1)
        trigger.set(1)
        assert snap_runs == 1
        assert seen_params == [{"via": "diamond"}]

    directed_graph = Graph("py-c18-directed-prune")
    directed_stimulus = ConformanceStimulus(directed_graph)
    directed_acc = directed_graph.state(0, name="acc")
    directed_trigger = directed_stimulus.state_empty("trigger")
    left_runs = 0
    right_runs = 0

    def left_snap(ctx) -> None:
        nonlocal left_runs
        left_runs += 1
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    def right_snap(ctx) -> None:
        nonlocal right_runs
        right_runs += 1
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    left = directed_graph.node([directed_acc], left_snap, name="left", pull_id="P")
    right = directed_graph.node([directed_acc], right_snap, name="right", pull_id="P")
    left_mid = directed_graph.node([left], lambda _ctx: None, name="left-mid", partial=True)
    right_mid = directed_graph.node([right], lambda _ctx: None, name="right-mid", partial=True)

    def directed_d(ctx) -> None:
        if _fresh_values(ctx, 0):
            ctx.request_pull_next("P", toward_dep=1)

    directed_demand = directed_graph.node(
        [directed_trigger, left_mid, right_mid],
        directed_d,
        name="d",
        partial=True,
    )
    with directed_demand.subscribe(lambda _msg: None):
        directed_acc.set(1)
        directed_trigger.set(1)
        assert left_runs == 1
        assert right_runs == 0


def test_c26_pull_family_public_params_resume_unknown_pause_and_data_up_negative():
    graph = Graph("py-c26-pull-explicit")
    stimulus = ConformanceStimulus(graph)
    a = stimulus.state_empty("a")
    b = stimulus.state_empty("b")
    trigger = stimulus.state_empty("trigger")
    seen_params: list[object | None] = []
    seen: list[Message[object]] = []
    alias_presence: dict[str, bool] = {}

    def snapshot(ctx) -> None:
        seen_params.append(ctx.pull_params())
        if ctx.has_data(0) and ctx.has_data(1):
            ctx.emit(ctx.data(0) + ctx.data(1))

    snap = graph.node([a, b], snapshot, name="snap", pull_id="snapshot")

    def requester(ctx) -> None:
        nonlocal alias_presence
        alias_presence = {name: hasattr(ctx, name) for name in ("up", "down")}
        trigger_values = _fresh_values(ctx, 0)
        if trigger_values:
            params = trigger_values[-1]
            pull_id = "unknown" if params == {"pull": "unknown"} else "snapshot"
            ctx.request_pull_next(pull_id, params, toward_dep=1)
        snap_values = _fresh_values(ctx, 1)
        if snap_values:
            ctx.emit(snap_values[-1])

    demand = graph.node([trigger, snap], requester, name="demand", partial=True)
    with demand.subscribe(seen.append):
        seen.clear()
        a.set(1)
        trigger.set({"cursor": 1})
        trigger.set({"cursor": 2})
        assert not any(isinstance(message, DataMessage) for message in seen)
        seen.clear()

        b.set(10)
        assert seen_params[-1] == {"cursor": 2}
        assert _kinds(seen)[-2:] == ["DIRTY", "DATA"]
        seen.clear()

        graph.resume(snap, "snapshot")
        trigger.set({"pull": "unknown"})
        assert not any(isinstance(message, DataMessage) for message in seen)
        assert alias_presence == {"up": False, "down": False}

    lock_graph = Graph("py-c26-pause-lock")
    lock_source = lock_graph.state(0, name="source")
    lock_node = lock_graph.node(
        [lock_source],
        lambda ctx: ctx.emit(ctx.data(0)) if ctx.has_data(0) else None,
        name="node",
    )
    lock_seen: list[Message[object]] = []
    with lock_node.subscribe(lock_seen.append):
        lock_seen.clear()
        lock_graph.pause(lock_node, "L")
        lock_source.set(1)
        assert not any(isinstance(message, DataMessage) for message in lock_seen)
        lock_graph.resume(lock_node, "L")
        assert isinstance(lock_seen[-1], DataMessage)

    bad_index_trigger = stimulus.state_empty("bad-index-trigger")
    bad_index = graph.node(
        [bad_index_trigger],
        lambda ctx: ctx.request_pull_next("snapshot", toward_dep=-1),
        name="bad-index",
        partial=True,
    )
    with bad_index.subscribe(lambda _msg: None):
        bad_index_trigger.set(1)
        assert bad_index.status == "errored"

    bad_high_trigger = stimulus.state_empty("bad-high-trigger")
    bad_high = graph.node(
        [bad_high_trigger],
        lambda ctx: ctx.request_pull_next("snapshot", toward_dep=ctx.dep_len),
        name="bad-high",
        partial=True,
    )
    bad_high_seen: list[Message[object]] = []
    with bad_high.subscribe(bad_high_seen.append):
        bad_high_trigger.set(1)
        errors = [msg for msg in bad_high_seen if isinstance(msg, ErrorMessage)]
        assert bad_high.status == "errored"
        assert "toward_dep" in errors[-1].error.message

    bad_trigger = stimulus.state_empty("bad-trigger")
    bad = graph.node([bad_trigger], lambda ctx: conformance.up_data_forbidden(ctx, 1))
    with bad.subscribe(lambda _msg: None):
        bad_trigger.set(1)
        assert bad.status == "errored"


def test_c27_pull_family_public_no_change_params_drive_output_and_plain_silence():
    graph = Graph("py-c27-pull-no-change")
    stimulus = ConformanceStimulus(graph)
    retained = graph.state(10, name="retained")
    trigger = stimulus.state_empty("trigger")
    seen_params: list[object | None] = []
    seen: list[Message[object]] = []

    def page(ctx) -> None:
        params = ctx.pull_params()
        seen_params.append(params)
        if isinstance(params, dict) and ctx.has_data(0):
            ctx.emit(ctx.data(0) + params["limit"])

    page_node = graph.node([retained], page, name="page", pull_id="page")

    def requester(ctx) -> None:
        trigger_values = _fresh_values(ctx, 0)
        if trigger_values:
            ctx.request_pull_next("page", trigger_values[-1], toward_dep=1)
        snap_values = _fresh_values(ctx, 1)
        if snap_values:
            ctx.emit(snap_values[-1])

    demand = graph.node([trigger, page_node], requester, name="demand", partial=True)
    with demand.subscribe(seen.append):
        seen.clear()
        trigger.set({"limit": 1})
        trigger.set({"limit": 2})

        data_values = [message.value for message in seen if isinstance(message, DataMessage)]
        assert seen_params[-2:] == [{"limit": 1}, {"limit": 2}]
        assert data_values[-2:] == [11, 12]
        seen.clear()

        graph.resume(page_node, "page")
        assert seen == []

    plain_graph = Graph("py-c27-plain-snapshot-silence")
    plain_stimulus = ConformanceStimulus(plain_graph)
    plain_retained = plain_graph.state(10, name="retained")
    plain_trigger = plain_stimulus.state_empty("trigger")
    plain_seen: list[Message[object]] = []
    plain_invocations = 0

    def plain_snapshot(ctx) -> None:
        nonlocal plain_invocations
        plain_invocations += 1
        fresh_values = [
            value
            for waves in ctx.wave_data[:1]
            for wave in waves
            for value in wave
            if value is not SENTINEL
        ]
        if fresh_values:
            ctx.emit(fresh_values[-1])

    plain = plain_graph.node([plain_retained], plain_snapshot, name="plain", pull_id="plain")

    def plain_requester(ctx) -> None:
        if _fresh_values(ctx, 0):
            ctx.request_pull_next("plain", toward_dep=1)
        snap_values = _fresh_values(ctx, 1)
        if snap_values:
            ctx.emit(snap_values[-1])

    plain_demand = plain_graph.node(
        [plain_trigger, plain],
        plain_requester,
        name="demand",
        partial=True,
    )
    with plain_demand.subscribe(plain_seen.append):
        plain_retained.set(11)
        plain_trigger.set(1)
        plain_trigger.set(2)
        data_values = [
            message.value for message in plain_seen if isinstance(message, DataMessage)
        ]
        assert plain_invocations == 2
        assert data_values == [11]


def test_d447_private_harness_preserves_facade_guards():
    assert "_conformance" not in graphrefly.__all__
    assert not hasattr(conformance._native, "ConformanceAsyncHandle")

    graph = Graph("py-d447-harness-guards")
    stimulus = ConformanceStimulus(graph)
    owned = graph.state(1, name="owned")
    other_graph = Graph("py-d447-other-graph")
    foreign = other_graph.state(1, name="foreign")

    with pytest.raises(GraphReflyRuntimeError, match="node must belong"):
        stimulus.node([foreign], lambda _ctx: None)

    with pytest.raises(GraphReflyValueError, match="pausable"):
        stimulus.node([owned], lambda _ctx: None, pausable="sometimes")

    assert graph.state(2, name="after-value-error").cache() == 2

    class AwaitableValue:
        def __await__(self):
            if False:
                yield None
            return None

    source, pending = stimulus.c10_true_mode_async_leaf_source(name="awaitable-source")
    with source.subscribe(lambda _msg: None):
        assert pending.has_pending()
        with pytest.raises(CallbackError, match="async callbacks"):
            pending.resolve(AwaitableValue())
        assert pending.has_pending()

    with pytest.raises(GraphReflyValueError, match="cannot be DATA"):
        stimulus.c17_dep_emits_data_then_completes(owned, SENTINEL)

    with pytest.raises(GraphReflyValueError, match="cannot be DATA"):
        stimulus.c23_dep_emits_data_data_invalidate(owned, 1, SENTINEL)

    with pytest.raises(GraphReflyValueError, match="cannot be DATA"):
        stimulus.c26_send_forbidden_data_up(owned, SENTINEL)


def test_c23_raw_ctx_wave_data_preserves_per_wave_distinctions():
    graph = Graph("py-c23-wave-data")
    stimulus = ConformanceStimulus(graph)
    a = graph.state(0, name="a")
    b = graph.state("initial", name="b")
    captures: list[list[list[list[object]]]] = []
    terminals: list[list[object]] = []
    alias_presence: dict[str, bool] = {}

    def body(ctx) -> None:
        nonlocal alias_presence
        captures.append(ctx.wave_data)
        terminals.append([ctx.terminal(0), ctx.terminal(1)])
        alias_presence = {
            name: hasattr(ctx, name)
            for name in ("latest", "prevData", "latestData", "depRecords")
        }

    node = graph.node([a, b], body, name="node", partial=True)
    with node.subscribe(lambda _msg: None):
        captures.clear()
        terminals.clear()

        b.set("b")
        assert captures[-1] == [[], [["b"]]]
        assert terminals[-1] == [False, False]
        assert alias_presence == {
            "latest": False,
            "prevData": False,
            "latestData": False,
            "depRecords": False,
        }

        stimulus.c23_dep_dirty_then_resolved(a)
        assert captures[-1] == [[[]], []]
        assert terminals[-1] == [False, False]

        stimulus.c23_dep_emits_data_data_invalidate(a, 1, 2)
        assert captures[-1] == [[[1, 2, SENTINEL]], []]
        assert captures[-1][0][0][2] is SENTINEL
        assert terminals[-1] == [False, False]

        a.set(None)
        assert captures[-1] == [[[None]], []]
        assert captures[-1][0][0][0] is None

        a.set([])
        assert captures[-1] == [[[[]]], []]


def test_c23_wave_data_can_drive_quiet_unread_dep_behavior():
    graph = Graph("py-c23-wave-data-quiet")
    a = graph.state(1, name="a")
    b = graph.state("initial", name="b")
    observed: list[Message[object]] = []
    captures: list[list[list[list[object]]]] = []

    def body(ctx) -> None:
        captures.append(ctx.wave_data)
        if ctx.wave_data[1] and not ctx.wave_data[0]:
            return
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    node = graph.node([a, b], body, name="node", partial=True)
    with node.subscribe(observed.append):
        observed.clear()
        captures.clear()

        b.set("b")

    assert captures[-1] == [[], [["b"]]]
    assert [msg.kind for msg in observed] == ["DIRTY", "RESOLVED"]


def test_c23_terminal_metadata_is_separate_from_wave_data():
    complete_graph = Graph("py-c23-complete-terminal")
    complete_stimulus = ConformanceStimulus(complete_graph)
    complete_source = complete_graph.state(1, name="source")
    complete_waves: list[list[list[list[object]]]] = []
    complete_terminals: list[object] = []

    def complete_body(ctx) -> None:
        complete_waves.append(ctx.wave_data)
        complete_terminals.append(ctx.terminal(0))

    complete_node = complete_graph.node(
        [complete_source],
        complete_body,
        name="node",
        partial=True,
        complete_when_deps_complete=False,
        terminal_as_real_input=True,
    )
    with complete_node.subscribe(lambda _msg: None):
        complete_waves.clear()
        complete_terminals.clear()
        complete_stimulus.c23_dep_completes(complete_source)

    assert complete_waves[-1] == [[]]
    assert complete_terminals[-1] is True

    error_graph = Graph("py-c23-error-terminal")
    error_stimulus = ConformanceStimulus(error_graph)
    error_source = error_graph.state(1, name="source")
    error_waves: list[list[list[list[object]]]] = []
    error_terminals: list[object] = []

    def error_body(ctx) -> None:
        error_waves.append(ctx.wave_data)
        error_terminals.append(ctx.terminal(0))

    error_node = error_graph.node(
        [error_source],
        error_body,
        name="node",
        partial=True,
        complete_when_deps_complete=False,
        error_when_deps_error=False,
        terminal_as_real_input=True,
    )
    with error_node.subscribe(lambda _msg: None):
        error_waves.clear()
        error_terminals.clear()
        error_stimulus.c23_dep_errors(error_source, "boom")

    assert error_waves[-1] == [[]]
    assert error_terminals[-1] == "boom"


def test_c23_python_sentinel_is_not_a_legal_data_payload():
    assert Sentinel() is SENTINEL

    graph = Graph("py-c23-sentinel-data")
    with pytest.raises(GraphReflyValueError, match="cannot be DATA"):
        graph.state(SENTINEL)

    source = graph.state(1, name="source")
    with pytest.raises(GraphReflyValueError, match="cannot be DATA"):
        source.set(SENTINEL)

    stimulus = ConformanceStimulus(graph)
    trigger = stimulus.state_empty("pull-trigger")
    pull_seen: list[Message[object]] = []
    pull_node = graph.node(
        [trigger],
        lambda ctx: ctx.request_pull_next("missing", SENTINEL),
        name="pull-sentinel",
        partial=True,
    )
    with pull_node.subscribe(pull_seen.append):
        trigger.set(1)
        assert isinstance(pull_seen[-1], ErrorMessage)
        assert "cannot be DATA" in pull_seen[-1].error.message

    with pytest.raises(GraphReflyValueError, match="cannot be DATA"):
        stimulus.c16_pull(pull_node, "missing", SENTINEL)

    emit_seen: list[Message[object]] = []

    def emit_sentinel(ctx) -> None:
        ctx.emit(SENTINEL)

    emit_node = graph.node([source], emit_sentinel, name="emit-sentinel", partial=True)
    with emit_node.subscribe(emit_seen.append):
        pass
    assert isinstance(emit_seen[-1], ErrorMessage)
    assert "cannot be DATA" in emit_seen[-1].error.message

    producer_seen: list[Message[object]] = []
    producer = graph.producer(lambda: SENTINEL, name="producer-sentinel")
    with producer.subscribe(producer_seen.append):
        pass
    assert isinstance(producer_seen[-1], ErrorMessage)
    assert "cannot be DATA" in producer_seen[-1].error.message

    derived_seen: list[Message[object]] = []
    derived = graph.derived([source], lambda _value: SENTINEL, name="derived-sentinel")
    with derived.subscribe(derived_seen.append):
        pass
    assert isinstance(derived_seen[-1], ErrorMessage)
    assert "cannot be DATA" in derived_seen[-1].error.message
