import gc
from importlib import import_module

import pytest

import graphrefly
from graphrefly import (
    CallbackError,
    ControlMessage,
    Ctx,
    DataMessage,
    ErrorMessage,
    Graph,
    GraphEvent,
    GraphReflyNoDataError,
    GraphReflyRuntimeError,
    GraphReflyValueError,
    Message,
    RewireNext,
    SubscriberCallbackError,
    Subscription,
)


def test_import_package_surface():
    assert graphrefly.__version__ == "0.21.0a0"
    assert graphrefly.version() == "0.21.0a0"
    assert Graph("smoke").describe()["name"] == "smoke"
    assert graphrefly.DataIssue("missing", "reserved").code == "missing"
    assert graphrefly.Ctx is Ctx
    assert graphrefly.RewireNext is RewireNext
    assert issubclass(graphrefly.GraphReflyNoDataError, LookupError)
    assert hasattr(import_module("graphrefly._native"), "Graph")
    assert "_conformance" not in graphrefly.__all__


def test_python_callback_runs_through_rust_graph_and_subscription_observes_wave():
    seen: list[Message[object]] = []
    graph = Graph("py-smoke")
    source = graph.state(1, name="source")
    plus_one = graph.derived([source], lambda value: value + 1, name="plus_one")

    with plus_one.subscribe(seen.append):
        assert plus_one.cache() == 2
        source.set(4)
        assert plus_one.cache() == 5
        assert plus_one.status in {"settled", "resolved"}

    assert DataMessage(2) in seen
    assert DataMessage(5) in seen


def test_callback_exception_becomes_graph_error_observation():
    seen: list[Message[object]] = []
    graph = Graph("py-error-smoke")
    source = graph.state(1, name="source")

    def boom(_value: int) -> int:
        raise ValueError("boom")

    bad = graph.derived([source], boom, name="bad")
    with bad.subscribe(seen.append):
        pass

    assert any(
        isinstance(msg, ErrorMessage)
        and msg.error.type_name == "ValueError"
        and msg.error.message == "boom"
        for msg in seen
    )


def test_async_callback_is_reported_as_graph_error():
    seen: list[Message[object]] = []
    graph = Graph("py-async-error-smoke")
    source = graph.state(1, name="source")

    async def async_callback(_value: int) -> int:
        return 2

    bad = graph.derived([source], async_callback, name="bad_async")
    with bad.subscribe(seen.append):
        pass

    assert any(
        isinstance(msg, ErrorMessage) and "async callbacks are deferred" in msg.error.message
        for msg in seen
    )


def test_batch_callback_exception_rolls_back_and_reraises_original_exception():
    graph = Graph("py-batch-smoke")
    source = graph.state(1, name="source")

    def mutate_then_raise() -> None:
        source.set(9)
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        graph.batch(mutate_then_raise)

    assert source.cache() == 1


def test_async_batch_callback_is_rejected_before_commit():
    graph = Graph("py-async-batch-smoke")
    source = graph.state(1, name="source")

    async def async_batch() -> None:
        source.set(9)

    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        graph.batch(async_batch)

    assert source.cache() == 1


def test_async_subscribe_callback_is_rejected_at_registration():
    graph = Graph("py-async-subscribe-smoke")
    source = graph.state(1, name="source")

    async def async_subscriber(_msg: Message[object]) -> None:
        pass

    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        source.subscribe(async_subscriber)


def test_none_is_valid_python_data_payload():
    seen: list[Message[object]] = []
    graph = Graph("py-none-smoke")
    source = graph.state(None, name="none_value")

    with source.subscribe(seen.append):
        pass

    assert source.cache() is None
    assert DataMessage(None) in seen
    node = next(node for node in graph.describe()["nodes"] if node["name"] == "none_value")
    assert node["has_value"] is True


def test_absent_cache_raises_without_conflating_cached_none():
    graph = Graph("py-no-data-smoke")
    source = graph.state(1, name="source")
    plus_one = graph.derived([source], lambda value: value + 1, name="plus_one")
    none_value = graph.state(None, name="none_value")

    assert plus_one.has_value is False
    with pytest.raises(GraphReflyNoDataError, match="no cached DATA"):
        plus_one.cache()
    assert plus_one.cache(default="missing") == "missing"

    assert none_value.has_value is True
    assert none_value.cache() is None


def test_advanced_node_ctx_emit_preserves_none_and_no_data_absence():
    graph = Graph("py-node-none-no-data-smoke")
    source = graph.state(1, name="source")
    none_node = graph.node([source], lambda ctx: ctx.emit(None), name="none_node")
    quiet_node = graph.node([source], lambda _ctx: None, name="quiet_node")

    with none_node.subscribe(lambda _msg: None), quiet_node.subscribe(lambda _msg: None):
        assert none_node.has_value is True
        assert none_node.cache() is None
        assert quiet_node.has_value is False
        with pytest.raises(GraphReflyNoDataError, match="no cached DATA"):
            quiet_node.cache()


def test_advanced_ctx_dep_presence_does_not_conflate_none_with_absence():
    graph = Graph("py-node-dep-presence-smoke")
    none_source = graph.state(None, name="none_source")
    trigger = graph.state(1, name="trigger")
    seen: list[tuple[bool, object, int]] = []

    def body(ctx: Ctx) -> None:
        seen.append((ctx.has_data(0), ctx.data(0, "missing"), ctx.data(1)))
        ctx.emit(seen[-1])

    node = graph.node([none_source, trigger], body, name="presence")
    with node.subscribe(lambda _msg: None):
        assert node.cache() == (True, None, 1)
        graph.invalidate(none_source)
        trigger.set(2)
        assert node.cache() == (False, "missing", 2)

    assert seen == [(True, None, 1), (False, "missing", 2)]


def test_advanced_node_decorator_form_uses_function_name_and_ctx():
    graph = Graph("py-node-decorator-smoke")
    source = graph.state(1, name="source")

    @graph.node([source])
    def plus_ten(ctx: Ctx) -> None:
        ctx.emit(ctx.data(0) + 10)

    with plus_ten.subscribe(lambda _msg: None):
        assert plus_ten.cache() == 11

    entry = next(node for node in graph.describe()["nodes"] if node["name"] == "plus_ten")
    assert entry["factory"] == "node"


def test_advanced_ctx_state_none_does_not_conflate_absence_when_has_state_is_checked():
    graph = Graph("py-node-state-none-smoke")
    source = graph.state(1, name="source")
    seen: list[tuple[bool, object | None]] = []

    def body(ctx: Ctx) -> None:
        seen.append((ctx.has_state, ctx.state))
        ctx.state = None
        ctx.emit(seen[-1])

    node = graph.node([source], body, name="state_none")
    with node.subscribe(lambda _msg: None):
        assert node.cache() == (False, None)
        source.set(2)
        assert node.cache() == (True, None)

    assert seen == [(False, None), (True, None)]


def test_advanced_node_async_callback_is_rejected_at_registration():
    graph = Graph("py-node-async-registration-smoke")
    source = graph.state(1, name="source")

    async def async_body(_ctx: Ctx) -> None:
        return None

    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        graph.node([source], async_body, name="async_node")

    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        @graph.node([source])
        async def async_decorated(_ctx: Ctx) -> None:
            return None


def test_advanced_ctx_multiple_emit_order_is_preserved():
    graph = Graph("py-node-multi-emit-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    def body(ctx: Ctx) -> None:
        ctx.emit(("first", ctx.data(0)))
        ctx.emit(("second", ctx.data(0)))

    node = graph.node([source], body, name="multi_emit")
    with node.subscribe(seen.append):
        assert node.cache() == ("second", 1)

    assert [msg.value for msg in seen if isinstance(msg, DataMessage)] == [
        ("first", 1),
        ("second", 1),
    ]


def test_advanced_ctx_index_out_of_range_raises_index_error():
    graph = Graph("py-node-index-error-smoke")
    source = graph.state(1, name="source")
    checked = False

    def body(ctx: Ctx) -> None:
        nonlocal checked
        with pytest.raises(IndexError):
            ctx.has_data(1)
        with pytest.raises(IndexError):
            ctx.data(1)
        checked = True
        ctx.emit(ctx.data(0))

    node = graph.node([source], body, name="index_error")
    with node.subscribe(lambda _msg: None):
        assert node.cache() == 1

    assert checked is True


def test_advanced_ctx_hook_async_callback_becomes_graph_error_without_leaking_coroutine(
    recwarn: pytest.WarningsRecorder,
):
    graph = Graph("py-node-async-hook-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    async def async_cleanup() -> None:
        return None

    def cleanup() -> object:
        return async_cleanup()

    def body(ctx: Ctx) -> None:
        ctx.on_invalidate(cleanup)
        ctx.emit(ctx.data(0))

    node = graph.node([source], body, name="async_hook")
    with node.subscribe(seen.append):
        seen.clear()
        graph.invalidate(source)

    gc.collect()
    assert any(
        isinstance(msg, ErrorMessage) and "async callbacks are deferred" in msg.error.message
        for msg in seen
    )
    assert not [
        warning
        for warning in recwarn
        if issubclass(warning.category, RuntimeWarning) and "never awaited" in str(warning.message)
    ]


def test_advanced_node_callback_exception_becomes_graph_error_observation():
    graph = Graph("py-node-error-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    def boom(_ctx: Ctx) -> None:
        raise ValueError("node boom")

    bad = graph.node([source], boom, name="bad")
    with bad.subscribe(seen.append):
        pass

    assert any(
        isinstance(msg, ErrorMessage)
        and msg.error.type_name == "ValueError"
        and msg.error.message == "node boom"
        for msg in seen
    )


def test_advanced_ctx_cleanup_hook_exception_becomes_graph_error():
    graph = Graph("py-node-hook-error-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    def flush() -> None:
        raise ValueError("hook boom")

    def body(ctx: Ctx) -> None:
        ctx.on_invalidate(flush)
        ctx.emit(ctx.data(0))

    node = graph.node([source], body, name="hook_error")
    with node.subscribe(seen.append):
        seen.clear()
        graph.invalidate(source)

    assert any(
        isinstance(msg, ErrorMessage)
        and msg.error.type_name == "ValueError"
        and msg.error.message == "hook boom"
        for msg in seen
    )


def test_advanced_ctx_commit_preserves_hook_order_before_emit_reentry():
    graph = Graph("py-node-hook-order-smoke")
    source = graph.state(1, name="source")
    events: list[str] = []
    subscription: list[Subscription] = []

    def cleanup() -> None:
        events.append("cleanup")

    def body(ctx: Ctx) -> None:
        ctx.on_deactivation(cleanup)
        ctx.emit(ctx.data(0))

    def observe(msg: Message[object]) -> None:
        if isinstance(msg, DataMessage) and msg.value == 2:
            events.append("data")
            subscription[0].unsubscribe()

    node = graph.node([source], body, name="hook_order")
    sub = node.subscribe(observe)
    subscription.append(sub)
    events.clear()

    source.set(2)

    assert events == ["data", "cleanup"]
    assert sub.closed is True


def test_advanced_ctx_is_inactive_during_commit_reentry():
    graph = Graph("py-node-ctx-scope-smoke")
    source = graph.state(1, name="source")
    stashed: list[Ctx] = []
    errors: list[GraphReflyRuntimeError] = []

    def body(ctx: Ctx) -> None:
        stashed.clear()
        stashed.append(ctx)
        ctx.emit(ctx.data(0))

    def observe(msg: Message[object]) -> None:
        if isinstance(msg, DataMessage) and msg.value == 2:
            with pytest.raises(GraphReflyRuntimeError) as exc_info:
                stashed[0].emit("late")
            errors.append(exc_info.value)

    node = graph.node([source], body, name="ctx_scope")
    with node.subscribe(observe):
        source.set(2)

    assert errors
    assert "ctx is only valid" in str(errors[0])


def test_advanced_node_fatal_base_exception_propagates_and_poisons_facade():
    graph = Graph("py-node-fatal-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    def boom(_ctx: Ctx) -> None:
        raise SystemExit("node exit")

    bad = graph.node([source], boom, name="bad")
    with pytest.raises(SystemExit, match="node exit"):
        bad.subscribe(seen.append)

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        bad.cache(default=None)
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_advanced_node_fatal_deactivation_hook_propagates_and_poisons_facade():
    graph = Graph("py-node-deactivation-fatal-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[object]] = []

    def cleanup() -> None:
        raise SystemExit("deactivation exit")

    def body(ctx: Ctx) -> None:
        ctx.on_deactivation(cleanup)
        ctx.emit(ctx.data(0))

    node = graph.node([source], body, name="fatal_cleanup")
    sub = node.subscribe(seen.append)

    with pytest.raises(SystemExit, match="deactivation exit"):
        sub.unsubscribe()

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        node.cache(default=None)
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_advanced_node_fatal_during_batch_commit_propagates_without_graph_error():
    graph = Graph("py-node-batch-fatal-smoke")
    source = graph.state(0, name="source")
    seen: list[Message[object]] = []

    def boom(ctx: Ctx) -> None:
        if ctx.data(0) == 1:
            raise SystemExit("node batch exit")
        ctx.emit(ctx.data(0))

    bad = graph.node([source], boom, name="bad")
    with pytest.raises(SystemExit, match="node batch exit"), bad.subscribe(seen.append):
        graph.batch(lambda: source.set(1))

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        bad.cache(default=None)
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_rewire_next_fatal_callback_propagates_without_graph_error():
    graph = Graph("py-rewire-next-fatal-smoke")
    source = graph.state(0, name="source")
    helper = graph.state("helper", name="helper")
    seen: list[Message[object]] = []

    def body(ctx: Ctx) -> None:
        if ctx.dep_len > 1 and ctx.has_data(1):
            raise SystemExit("rewire next exit")
        if ctx.has_data(0) and ctx.data(0) == 1:
            ctx.rewire_next.subscribe_dep(helper, body)
        ctx.emit(ctx.data(0))

    node = graph.node([source], body, name="rewire-next-fatal", partial=True)
    with pytest.raises(SystemExit, match="rewire next exit"), node.subscribe(seen.append):
        source.set(1)

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        node.cache(default=None)
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_fatal_poison_preserves_original_exception_when_teardown_also_fails():
    graph = Graph("py-fatal-teardown-mask-smoke")
    source = graph.state(1, name="source")

    def cleanup() -> None:
        raise SystemExit("cleanup exit")

    def cleanup_body(ctx: Ctx) -> None:
        ctx.on_deactivation(cleanup)
        ctx.emit(ctx.data(0))

    cleanup_node = graph.node([source], cleanup_body, name="cleanup")
    cleanup_subscription = cleanup_node.subscribe(lambda _msg: None)

    def original(_ctx: Ctx) -> None:
        raise SystemExit("original exit")

    fatal_node = graph.node([source], original, name="fatal")
    with pytest.raises(SystemExit, match="original exit"):
        fatal_node.subscribe(lambda _msg: None)

    assert graph.closed is True
    assert cleanup_subscription.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        source.cache()


def test_decorators_and_context_manager_are_explicit_graph_owned_sugar():
    with Graph("py-decorator-smoke") as graph:
        source = graph.state(1, name="source")

        @graph.derived([source])
        def plus_one(value: int) -> int:
            return value + 1

        effects: list[int] = []

        @graph.effect([plus_one])
        def record(value: int) -> None:
            effects.append(value)

        with record.subscribe(lambda _msg: None):
            assert plus_one.cache() == 2
            source.set(4)
            assert plus_one.cache() == 5
            assert effects[-1] == 5

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="graph is closed"):
        plus_one.cache()


def test_subscriber_callback_errors_are_captured_at_python_boundary():
    graph = Graph("py-subscriber-error-smoke")
    source = graph.state(1, name="source")
    captured: list[SubscriberCallbackError] = []

    def subscriber(_msg: Message[int]) -> None:
        raise ValueError("observer boom")

    sub = source.subscribe(subscriber, on_error=captured.append)

    assert captured
    assert sub.callback_errors == tuple(captured)
    assert isinstance(captured[0].original, ValueError)
    assert captured[0].original.__traceback__ is None
    sub.unsubscribe()


def test_subscriber_fatal_base_exception_propagates_without_boundary_wrapping():
    graph = Graph("py-subscriber-fatal-smoke")
    source = graph.state(1, name="source")
    events: list[GraphEvent] = []

    def subscriber(_msg: Message[int]) -> None:
        raise SystemExit("exit")

    observer = graph.observe(events.append)
    with pytest.raises(SystemExit, match="exit"):
        source.subscribe(subscriber)

    assert graph.closed is True
    assert observer.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        source.cache()
    assert not any(isinstance(event.message, ErrorMessage) for event in events)


def test_subscriber_keyboard_interrupt_propagates_without_boundary_wrapping():
    graph = Graph("py-subscriber-keyboard-interrupt-smoke")
    source = graph.state(1, name="source")
    events: list[GraphEvent] = []

    def subscriber(_msg: Message[int]) -> None:
        raise KeyboardInterrupt

    observer = graph.observe(events.append)
    with pytest.raises(KeyboardInterrupt):
        source.subscribe(subscriber)

    assert graph.closed is True
    assert observer.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        source.cache()
    assert not any(isinstance(event.message, ErrorMessage) for event in events)


def test_subscriber_callback_errors_keep_bounded_history():
    graph = Graph("py-subscriber-error-history-smoke")
    source = graph.state(0, name="source")

    def subscriber(_msg: Message[int]) -> None:
        raise ValueError("observer boom")

    sub = source.subscribe(subscriber)
    for value in range(40):
        source.set(value)

    assert len(sub.callback_errors) == 32
    assert all(error.original.__traceback__ is None for error in sub.callback_errors)
    sub.unsubscribe()


def test_subscriber_on_error_failures_are_captured_at_python_boundary():
    graph = Graph("py-subscriber-on-error-smoke")
    source = graph.state(1, name="source")
    raised = False

    def subscriber(_msg: Message[int]) -> None:
        nonlocal raised
        if not raised:
            raised = True
            raise ValueError("observer boom")

    def on_error(_error: SubscriberCallbackError) -> None:
        raise RuntimeError("handler boom")

    sub = source.subscribe(subscriber, on_error=on_error)

    assert len(sub.callback_errors) == 2
    assert isinstance(sub.callback_errors[0].original, ValueError)
    assert isinstance(sub.callback_errors[1].original, RuntimeError)
    sub.unsubscribe()


def test_observe_callback_errors_are_captured_at_python_boundary():
    graph = Graph("py-observe-error-smoke")
    source = graph.state(1, name="source")
    captured: list[SubscriberCallbackError] = []

    def observer(event: GraphEvent) -> None:
        if event.message == DataMessage(2):
            raise ValueError("observe boom")

    sub = graph.observe(observer, on_error=captured.append)
    source.set(2)

    assert captured
    assert sub.callback_errors == tuple(captured)
    assert isinstance(captured[0].original, ValueError)
    assert captured[0].original.__traceback__ is None
    sub.unsubscribe()


def test_observe_fatal_base_exception_propagates_to_initiating_call():
    graph = Graph("py-observe-fatal-smoke")
    source = graph.state(1, name="source")

    def observer(event: GraphEvent) -> None:
        if event.message == DataMessage(2):
            raise SystemExit("observe exit")

    sub = graph.observe(observer)
    with pytest.raises(SystemExit, match="observe exit"):
        source.set(2)

    assert graph.closed is True
    assert sub.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        source.cache()


def test_observe_fatal_during_registration_propagates_without_graph_error():
    graph = Graph("py-observe-eager-fatal-smoke")
    source = graph.state(1, name="source")
    source.subscribe(lambda _msg: None)
    calls = 0

    def observer(_event: GraphEvent) -> None:
        nonlocal calls
        calls += 1
        raise SystemExit("observe eager exit")

    with pytest.raises(SystemExit, match="observe eager exit"):
        graph.observe(observer)

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        source.cache()
    assert calls == 1


def test_graph_observe_uses_typed_message_shape():
    seen: list[GraphEvent] = []
    graph = Graph("py-observe-shape-smoke")
    source = graph.state(1, name="source")
    graph.derived([source], lambda value: value + 1, name="plus_one")

    with graph.observe(seen.append):
        source.set(4)

    assert any(
        event.path.endswith("plus_one")
        and event.message == DataMessage(5)
        and event.tier == 3
        for event in seen
    )


def test_describe_exposes_factory_metadata_without_raw_function_bodies():
    graph = Graph("py-describe-smoke")
    source = graph.state(1, name="source")
    plus_one = graph.derived([source], lambda value: value + 1, name="plus_one")

    sub = plus_one.subscribe(lambda _msg: None)
    snapshot = graph.describe()
    sub.unsubscribe()
    nodes = {node["name"]: node for node in snapshot["nodes"]}

    assert nodes["source"]["factory"] == "state"
    assert nodes["plus_one"]["factory"] == "derived"
    assert nodes["plus_one"]["has_value"] is True
    assert "lambda" not in repr(snapshot)


def test_public_value_and_runtime_errors_are_facade_exceptions():
    graph = Graph("py-error-boundary-smoke")

    graph.state(1, name="same")
    with pytest.raises(GraphReflyRuntimeError, match="duplicate graph node id"):
        graph.state(2, name="same")

    source = graph.state(1, name="source")
    derived = graph.derived([source], lambda value: value + 1, name="derived")
    with pytest.raises(GraphReflyRuntimeError, match="state nodes"):
        derived.set(3)


def test_public_control_facade_is_graph_owned_and_validates_lock_id():
    graph = Graph("py-control-facade-smoke")
    other_graph = Graph("py-control-other-graph")
    source = graph.state(1, name="source")
    other_source = other_graph.state(1, name="other_source")
    derived = graph.derived([source], lambda value: value + 1, name="derived")
    seen: list[Message[int]] = []

    with derived.subscribe(seen.append):
        assert derived.cache() == 2
        graph.pause(derived, "lock")
        source.set(2)
        assert derived.cache() == 2
        graph.resume(derived, "lock")
        assert derived.cache() == 3
        seen.clear()
        graph.invalidate(derived)

    assert ControlMessage("INVALIDATE") in seen
    with pytest.raises(GraphReflyRuntimeError, match="must belong"):
        graph.invalidate(other_source)
    with pytest.raises(GraphReflyValueError, match="lock_id must be a str"):
        graph.pause(derived, object())  # type: ignore[arg-type]


def test_graph_callback_fatal_base_exception_propagates_without_graph_error():
    graph = Graph("py-graph-fatal-smoke")
    source = graph.state(1, name="source")

    def boom(_value: int) -> int:
        raise SystemExit("graph exit")

    bad = graph.derived([source], boom, name="bad")
    with pytest.raises(SystemExit, match="graph exit"):
        bad.subscribe(lambda _msg: None)

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        bad.cache(default=None)


def test_graph_callback_keyboard_interrupt_propagates_without_graph_error():
    graph = Graph("py-graph-keyboard-interrupt-smoke")
    source = graph.state(1, name="source")

    def boom(_value: int) -> int:
        raise KeyboardInterrupt

    bad = graph.derived([source], boom, name="bad")
    with pytest.raises(KeyboardInterrupt):
        bad.subscribe(lambda _msg: None)

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        bad.cache(default=None)


def test_graph_callback_fatal_during_batch_commit_propagates_without_graph_error():
    graph = Graph("py-batch-commit-fatal-smoke")
    source = graph.state(0, name="source")
    seen: list[Message[object]] = []

    def boom(value: int) -> int:
        if value == 1:
            raise SystemExit("batch commit exit")
        return value

    bad = graph.derived([source], boom, name="bad")
    with pytest.raises(SystemExit, match="batch commit exit"), bad.subscribe(seen.append):
        graph.batch(lambda: source.set(1))

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        bad.cache(default=None)
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_graph_callback_keyboard_interrupt_during_batch_commit_stays_fatal():
    graph = Graph("py-batch-commit-keyboard-interrupt-smoke")
    source = graph.state(0, name="source")
    seen: list[Message[object]] = []

    def boom(value: int) -> int:
        if value == 1:
            raise KeyboardInterrupt
        return value

    bad = graph.derived([source], boom, name="bad")
    with pytest.raises(KeyboardInterrupt), bad.subscribe(seen.append):
        graph.batch(lambda: source.set(1))

    assert graph.closed is True
    with pytest.raises(GraphReflyRuntimeError, match="fatal host boundary abort"):
        bad.cache(default=None)
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)


def test_graph_close_releases_facade_subscriptions_and_rejects_later_use():
    graph = Graph("py-close-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[int]] = []
    sub = source.subscribe(seen.append)

    assert sub.closed is False
    graph.close()
    graph.close()

    assert graph.closed is True
    assert sub.closed is True
    sub.unsubscribe()
    with pytest.raises(GraphReflyRuntimeError, match="graph is closed"):
        source.cache()
    with pytest.raises(GraphReflyRuntimeError, match="graph is closed"):
        graph.state(2, name="after_close")


def test_dropped_subscription_handle_releases_native_subscription():
    graph = Graph("py-subscription-drop-smoke")
    source = graph.state(1, name="source")
    seen: list[Message[int]] = []

    sub = source.subscribe(seen.append)
    assert DataMessage(1) in seen
    del sub
    gc.collect()

    source.set(2)

    assert DataMessage(2) not in seen


def test_callback_error_class_is_public_taxonomy_for_future_mapping():
    assert issubclass(CallbackError, graphrefly.GraphReflyError)
