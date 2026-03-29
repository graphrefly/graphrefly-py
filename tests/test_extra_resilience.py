"""Roadmap Phase 3.1 — resilience utils (retry, backoff, breaker, rate, status, checkpoint)."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from graphrefly import Graph, Messages, MessageType, node, pipe, state
from graphrefly.extra.backoff import (
    BackoffPreset,
    constant,
    exponential,
    fibonacci,
    linear,
    resolve_backoff_preset,
)
from graphrefly.extra.checkpoint import (
    DictCheckpointAdapter,
    FileCheckpointAdapter,
    MemoryCheckpointAdapter,
    SqliteCheckpointAdapter,
    checkpoint_node_value,
    restore_graph_checkpoint,
    save_graph_checkpoint,
)
from graphrefly.extra.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    TokenBucket,
    rate_limiter,
    retry,
    token_tracker,
    with_breaker,
    with_status,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from graphrefly.core.node import NodeActions


def _values(sink: list[Messages]) -> list[Any]:
    out: list[Any] = []
    for batch in sink:
        for m in batch:
            if m[0] is MessageType.DATA:
                out.append(m[1])
    return out


def _has_error(sink: list[Messages]) -> bool:
    return any(m[0] is MessageType.ERROR for batch in sink for m in batch)


def _errors_then_ok(*, errors_before_ok: int = 2) -> Any:
    """Each subscription increments n; first ``errors_before_ok`` runs emit ``ERROR``."""

    n = [0]

    def start(_deps: list[Any], actions: NodeActions) -> Callable[[], None]:
        n[0] += 1
        if n[0] <= errors_before_ok:
            actions.down([(MessageType.ERROR, ValueError(f"round {n[0]}"))])
        else:
            actions.emit("recovered")
        return lambda: None

    return node(
        start,
        describe_kind="flapping",
        complete_when_deps_complete=False,
        resubscribable=True,
    )


def test_exponential_backoff() -> None:
    b = exponential(base=1.0, factor=2.0, max_delay=10.0, jitter="none")
    assert b(0, None, None) == 1.0
    assert b(1, None, None) == 2.0
    assert b(2, None, None) == 4.0


def test_linear_backoff() -> None:
    b = linear(base=0.5, step=0.5)
    assert b(0, None, None) == 0.5
    assert b(1, None, None) == 1.0


def test_fibonacci_backoff() -> None:
    b = fibonacci(base=1.0, max_delay=100.0)
    assert b(0, None, None) == 1.0
    assert b(1, None, None) == 2.0
    assert b(2, None, None) == 3.0
    assert b(3, None, None) == 5.0


def test_resolve_backoff_preset() -> None:
    s = resolve_backoff_preset("linear")
    assert s(0, None, None) == 1.0
    with pytest.raises(ValueError, match="Unknown backoff"):
        resolve_backoff_preset(cast("BackoffPreset", "nope"))


def test_retry_zero_forwards_error() -> None:
    src = _errors_then_ok(errors_before_ok=5)
    sink: list[Messages] = []
    out = pipe(src, retry(0))
    out.subscribe(sink.append)
    time.sleep(0.08)
    assert _has_error(sink)
    assert _values(sink) == []


def test_retry_recovers_with_backoff() -> None:
    src = _errors_then_ok(errors_before_ok=2)
    sink: list[Messages] = []
    bo = exponential(base=0.01, factor=2.0, max_delay=0.05, jitter="none")
    out = pipe(src, retry(2, backoff=bo))
    out.subscribe(sink.append)
    time.sleep(0.35)
    assert _values(sink) == ["recovered"]
    assert not _has_error(sink)


def test_retry_exhausted() -> None:
    src = _errors_then_ok(errors_before_ok=5)
    sink: list[Messages] = []
    out = pipe(src, retry(1, backoff=constant(0.0)))
    out.subscribe(sink.append)
    time.sleep(0.15)
    assert _has_error(sink)
    assert _values(sink) == []


def test_rate_limiter_queues_then_drains() -> None:
    s = state(0)
    sink: list[Messages] = []
    out = pipe(s, rate_limiter(2, 0.06))
    out.subscribe(sink.append)
    for i in range(1, 5):
        s.down([(MessageType.DATA, i)])
    time.sleep(0.02)
    mid = _values(sink)
    assert mid == [1, 2]
    time.sleep(0.12)
    assert _values(sink) == [1, 2, 3, 4]


def test_circuit_breaker_opens() -> None:
    b = CircuitBreaker(failure_threshold=2)
    assert b.can_execute()
    b.record_failure(ValueError("a"))
    assert b.state == "closed"
    b.record_failure(ValueError("b"))
    assert b.state == "open"
    assert not b.can_execute()


def test_with_breaker_errors_when_open() -> None:
    b = CircuitBreaker(failure_threshold=1, cooldown=60.0)
    b.record_failure(ValueError("trip"))
    src = state(1)
    bundle = with_breaker(b, on_open="error")(src)
    sink: list[Messages] = []
    bundle.node.subscribe(sink.append)
    src.down([(MessageType.DATA, 42)])
    assert any(
        m[0] is MessageType.ERROR and isinstance(m[1], CircuitOpenError)
        for batch in sink
        for m in batch
    )


def test_with_breaker_skip_resolved_when_open() -> None:
    b = CircuitBreaker(failure_threshold=1, cooldown=60.0)
    b.record_failure(ValueError("trip"))
    src = state(1)
    bundle = with_breaker(b, on_open="skip")(src)
    sink: list[Messages] = []
    bundle.node.subscribe(sink.append)
    src.down([(MessageType.DATA, 42)])
    assert any(m[0] is MessageType.RESOLVED for batch in sink for m in batch)


def test_token_bucket() -> None:
    tb = token_tracker(2.0, refill_per_second=0.0)
    assert isinstance(tb, TokenBucket)
    assert tb.try_consume(1.0)
    assert tb.try_consume(1.0)
    assert not tb.try_consume(0.1)


def test_with_status_error_and_active() -> None:
    s = state(1)
    w = with_status(s, initial_status="pending")
    sink: list[Messages] = []
    w.node.subscribe(sink.append)
    assert w.status.get() == "pending"
    s.down([(MessageType.DATA, 2)])
    assert w.status.get() == "active"
    assert w.node.get() == 2
    s.down([(MessageType.ERROR, ValueError("e"))])
    assert w.status.get() == "errored"
    assert isinstance(w.error.get(), ValueError)


def test_memory_checkpoint_graph_round_trip() -> None:
    g = Graph("g")
    a = state(10)
    g.add("a", a)
    adapter = MemoryCheckpointAdapter()
    save_graph_checkpoint(g, adapter)
    g.set("a", 99)
    assert g.get("a") == 99
    assert restore_graph_checkpoint(g, adapter) is True
    assert g.get("a") == 10
    assert restore_graph_checkpoint(g, adapter) is True


def test_dict_checkpoint_adapter() -> None:
    store: dict[str, Any] = {}
    ad = DictCheckpointAdapter(store, key="ck")
    g = Graph("x")
    g.add("n", state("hi"))
    save_graph_checkpoint(g, ad)
    assert "ck" in store
    g.set("n", "bye")
    restore_graph_checkpoint(g, ad)
    assert g.get("n") == "hi"


def test_file_checkpoint_adapter_round_trip() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "c.json"
        ad = FileCheckpointAdapter(path)
        g = Graph("fg")
        g.add("z", state({"k": 1}))
        save_graph_checkpoint(g, ad)
        g.set("z", {"k": 2})
        assert restore_graph_checkpoint(g, ad) is True
        assert g.get("z") == {"k": 1}
        assert json.loads(path.read_text(encoding="utf-8"))["name"] == "fg"


def test_checkpoint_node_value() -> None:
    n = state({"a": 1})
    assert checkpoint_node_value(n) == {"version": 1, "value": {"a": 1}}


def test_sqlite_checkpoint_adapter_round_trip() -> None:
    with tempfile.TemporaryDirectory() as d:
        db_path = Path(d) / "test.db"
        ad = SqliteCheckpointAdapter(db_path)
        g = Graph("g")
        g.add("z", state(99))
        save_graph_checkpoint(g, ad)
        g2 = Graph("g")
        g2.add("z", state(0))
        assert restore_graph_checkpoint(g2, ad) is True
        assert g2.get("z") == 99
        ad.close()


def test_with_breaker_meta_integration() -> None:
    b = CircuitBreaker(failure_threshold=2)
    src = state(1)
    bundle = with_breaker(b)(src)
    assert bundle.node.meta["breaker_state"] is bundle.breaker_state
    assert bundle.breaker_state.get() == "closed"


def test_with_status_meta_integration() -> None:
    s = state(0)
    bundle = with_status(s)
    assert bundle.node.meta["status"] is bundle.status
    assert bundle.node.meta["error"] is bundle.error
    assert bundle.status.get() == "pending"


def test_with_breaker_appears_in_graph_describe() -> None:
    b = CircuitBreaker(failure_threshold=2)
    s = state(1)
    bundle = with_breaker(b)(s)
    g = Graph("test")
    g.add("src", s)
    g.add("guarded", bundle.node)
    desc = g.describe()
    meta_path = "guarded::__meta__::breaker_state"
    assert meta_path in desc["nodes"]
    assert desc["nodes"][meta_path]["value"] == "closed"


def test_with_status_appears_in_graph_describe() -> None:
    s = state(0)
    bundle = with_status(s)
    g = Graph("test")
    g.add("src", s)
    g.add("tracked", bundle.node)
    desc = g.describe()
    assert "tracked::__meta__::status" in desc["nodes"]
    assert desc["nodes"]["tracked::__meta__::status"]["value"] == "pending"
    assert "tracked::__meta__::error" in desc["nodes"]
    assert desc["nodes"]["tracked::__meta__::error"]["value"] is None


def test_restore_empty_returns_false() -> None:
    g = Graph("empty")
    g.add("a", state(0))
    assert restore_graph_checkpoint(g, MemoryCheckpointAdapter()) is False
