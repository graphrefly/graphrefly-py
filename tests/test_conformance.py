from graphrefly import ControlMessage, ErrorMessage, Graph, Message, _native


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
