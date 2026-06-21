import pytest

import graphrefly
from graphrefly import CallbackError, Graph, GraphReflyRuntimeError, GraphReflyValueError, Message


def test_import_package_surface():
    assert graphrefly.__version__ == "0.21.0a0"
    assert graphrefly.version() == "0.21.0a0"
    assert Graph("smoke").describe()["name"] == "smoke"


def test_python_callback_runs_through_rust_graph_and_subscription_observes_wave():
    seen: list[Message] = []
    graph = Graph("py-smoke")
    source = graph.state(1, name="source")
    plus_one = graph.derived([source], lambda value: value + 1, name="plus_one")

    with plus_one.subscribe(seen.append):
        assert plus_one.cache() == 2
        source.set(4)
        assert plus_one.cache() == 5
        assert plus_one.status in {"settled", "resolved"}

    assert Message("DATA", 2) in seen
    assert Message("DATA", 5) in seen


def test_callback_exception_becomes_graph_error_observation():
    seen: list[Message] = []
    graph = Graph("py-error-smoke")
    source = graph.state(1, name="source")

    def boom(_value: int) -> int:
        raise ValueError("boom")

    bad = graph.derived([source], boom, name="bad")
    with bad.subscribe(seen.append):
        pass

    assert any(msg.kind == "ERROR" and "ValueError: boom" in str(msg.value) for msg in seen)


def test_async_callback_is_reported_as_graph_error():
    seen: list[Message] = []
    graph = Graph("py-async-error-smoke")
    source = graph.state(1, name="source")

    async def async_callback(_value: int) -> int:
        return 2

    bad = graph.derived([source], async_callback, name="bad_async")
    with bad.subscribe(seen.append):
        pass

    assert any(
        msg.kind == "ERROR" and "async callbacks are deferred" in str(msg.value) for msg in seen
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

    async def async_subscriber(_msg: Message) -> None:
        pass

    with pytest.raises(GraphReflyRuntimeError, match="async callbacks are deferred"):
        source.subscribe(async_subscriber)


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

    with pytest.raises(GraphReflyValueError):
        graph.state(None, name="none_is_sentinel")

    graph.state(1, name="same")
    with pytest.raises(GraphReflyRuntimeError, match="duplicate graph node id"):
        graph.state(2, name="same")

    source = graph.state(1, name="source")
    derived = graph.derived([source], lambda value: value + 1, name="derived")
    with pytest.raises(GraphReflyRuntimeError, match="state nodes"):
        derived.set(3)


def test_callback_error_class_is_public_taxonomy_for_future_mapping():
    assert issubclass(CallbackError, graphrefly.GraphReflyError)
