import pytest

from graphrefly import (
    SENTINEL,
    ControlMessage,
    ErrorMessage,
    Graph,
    GraphReflyValueError,
    Message,
    Sentinel,
    _native,
)


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
    graph = _native.Graph("py-c7-native-negative-controls")
    source = graph.state(5, "source")
    derived = graph.derived([source], lambda value: value, "derived")
    seen: list[tuple[str, object]] = []
    sub = derived.subscribe(lambda kind, value: seen.append((kind, value)))

    assert derived.cache() == 5
    seen.clear()

    derived._up_dirty()
    assert source.cache_entry() == (True, 5)
    assert source.status() == "settled"
    assert seen == []

    derived._up_teardown()
    assert source.cache_entry() == (True, 5)
    assert source.status() == "settled"
    assert seen == []
    sub.unsubscribe()


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


def test_c15_dep_complete_releases_dirty_and_joins_once():
    graph = Graph("py-c15-complete-mid-dirty")
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

        b._native._down_dirty()
        assert d.status == "dirty"
        c.set(20)
        assert runs == 0
        assert d.cache() == 11
        b._native._down_complete()

        assert _kinds(seen) == ["DIRTY", "DATA"]
        assert runs == 1
        assert d.cache() == 21
        assert d.status != "completed"


def test_c15_dep_complete_sole_dirty_contributor_un_dirties_without_data():
    graph = Graph("py-c15-sole-dirty-complete")
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

        b._native._down_dirty()
        b._native._down_complete()

        assert _kinds(seen) == ["DIRTY", "RESOLVED"]
        assert d.cache() == 11
        assert d.status == "resolved"
        assert d.status != "completed"


def test_c15_absorbed_error_releases_dirty_and_can_read_terminal():
    graph = Graph("py-c15-rescue-error")
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

        b._native._down_dirty()
        b._native._down_error("boom")

        assert _kinds(seen) == ["DIRTY", "DATA"]
        assert not any(isinstance(message, ErrorMessage) for message in seen)
        assert d.status != "errored"
        assert d.status != "completed"
        assert d.cache() == 10


def test_c15_gate_holds_terminal_dirty_release_un_dirties_without_running():
    graph = Graph("py-c15-gate-holds")
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

        c._native._down_dirty()
        b.set(5)
        assert runs == 0
        c._native._down_complete()

        assert runs == 0
        assert _kinds(seen) == ["DIRTY", "RESOLVED"]
        assert d.has_value is False


def test_c17_absorbed_error_then_complete_auto_completes():
    graph = Graph("py-c17-error-then-complete")
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
        c._native._down_error("boom")
        assert d.status != "completed"
        assert d.status != "errored"

        b._native._down_data_complete(1)

        assert _kinds(seen).count("COMPLETE") == 1
        assert d.status == "completed"


def test_c17_complete_then_absorbed_error_auto_completes_order_independent():
    graph = Graph("py-c17-complete-then-error")
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
        b._native._down_data_complete(1)
        assert _kinds(seen).count("COMPLETE") == 0
        assert d.status != "completed"
        assert d.status != "errored"

        c._native._down_error("boom")

        assert _kinds(seen).count("COMPLETE") == 1
        assert d.status == "completed"


def test_c17_default_error_cascade_does_not_take_absorbed_path():
    graph = Graph("py-c17-default-error-cascade")
    b = graph.node([], lambda _ctx: None, name="empty-b")
    c = graph.node([], lambda _ctx: None, name="empty-c")
    seen: list[Message[object]] = []

    def forward_b(ctx) -> None:
        if ctx.has_data(0):
            ctx.emit(ctx.data(0))

    d = graph.node([b, c], forward_b, name="d")

    with d.subscribe(seen.append):
        seen.clear()
        c._native._down_error("boom")

        assert any(isinstance(message, ErrorMessage) for message in seen)
        assert ControlMessage("COMPLETE") not in seen
        assert d.status == "errored"


def test_c23_raw_ctx_wave_data_preserves_per_wave_distinctions():
    graph = Graph("py-c23-wave-data")
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

        a._native._up_dirty()
        a._native._down_resolved()
        assert captures[-1] == [[[]], []]
        assert terminals[-1] == [False, False]

        a._native._down_data_data_invalidate(1, 2)
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
        complete_source._native._down_complete()

    assert complete_waves[-1] == [[]]
    assert complete_terminals[-1] is True

    error_graph = Graph("py-c23-error-terminal")
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
        error_source._native._down_error("boom")

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
