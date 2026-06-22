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
