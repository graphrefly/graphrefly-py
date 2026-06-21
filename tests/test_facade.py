from importlib import import_module

import pytest

import graphrefly
from graphrefly import (
    CallbackError,
    DataMessage,
    ErrorMessage,
    Graph,
    GraphEvent,
    GraphReflyRuntimeError,
    Message,
    SubscriberCallbackError,
)


def test_import_package_surface():
    assert graphrefly.__version__ == "0.21.0a0"
    assert graphrefly.version() == "0.21.0a0"
    assert Graph("smoke").describe()["name"] == "smoke"
    assert hasattr(import_module("graphrefly._native"), "Graph")


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


def test_callback_error_class_is_public_taxonomy_for_future_mapping():
    assert issubclass(CallbackError, graphrefly.GraphReflyError)
