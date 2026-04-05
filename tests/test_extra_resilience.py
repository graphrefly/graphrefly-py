"""Roadmap Phase 3.1 — resilience utils (retry, backoff, breaker, rate, status, checkpoint)."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from graphrefly import Graph, Messages, MessageType, node, state
from graphrefly.extra.backoff import (
    NS_PER_SEC,
    BackoffPreset,
    constant,
    decorrelated_jitter,
    exponential,
    fibonacci,
    linear,
    resolve_backoff_preset,
    with_max_attempts,
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
    cache,
    circuit_breaker,
    fallback,
    rate_limiter,
    retry,
    timeout,
    token_bucket,
    token_tracker,
    with_breaker,
    with_status,
)
from graphrefly.extra.resilience import (
    TimeoutError as ResTimeoutError,
)
from graphrefly.extra.sources import never, of, throw_error

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
    b = exponential(base_ns=NS_PER_SEC, factor=2.0, max_delay_ns=10 * NS_PER_SEC, jitter="none")
    assert b(0, None, None) == NS_PER_SEC
    assert b(1, None, None) == 2 * NS_PER_SEC
    assert b(2, None, None) == 4 * NS_PER_SEC


def test_linear_backoff() -> None:
    b = linear(base_ns=500_000_000, step_ns=500_000_000)
    assert b(0, None, None) == 500_000_000
    assert b(1, None, None) == 1_000_000_000


def test_fibonacci_backoff() -> None:
    b = fibonacci(base_ns=NS_PER_SEC, max_delay_ns=100 * NS_PER_SEC)
    assert b(0, None, None) == NS_PER_SEC
    assert b(1, None, None) == 2 * NS_PER_SEC
    assert b(2, None, None) == 3 * NS_PER_SEC
    assert b(3, None, None) == 5 * NS_PER_SEC


def test_resolve_backoff_preset() -> None:
    s = resolve_backoff_preset("linear")
    assert s(0, None, None) == NS_PER_SEC
    with pytest.raises(ValueError, match="Unknown backoff"):
        resolve_backoff_preset(cast("BackoffPreset", "nope"))


def test_retry_zero_forwards_error() -> None:
    src = _errors_then_ok(errors_before_ok=5)
    sink: list[Messages] = []
    out = retry(src, 0)
    out.subscribe(sink.append)
    time.sleep(0.08)
    assert _has_error(sink)
    assert _values(sink) == []


def test_retry_recovers_with_backoff() -> None:
    src = _errors_then_ok(errors_before_ok=2)
    sink: list[Messages] = []
    # 10ms base in nanoseconds
    bo = exponential(base_ns=10_000_000, factor=2.0, max_delay_ns=50_000_000, jitter="none")
    out = retry(src, 2, backoff=bo)
    out.subscribe(sink.append)
    time.sleep(0.35)
    assert _values(sink) == ["recovered"]
    assert not _has_error(sink)


def test_retry_exhausted() -> None:
    src = _errors_then_ok(errors_before_ok=5)
    sink: list[Messages] = []
    out = retry(src, 1, backoff=constant(0))
    out.subscribe(sink.append)
    time.sleep(0.15)
    assert _has_error(sink)
    assert _values(sink) == []


def test_rate_limiter_queues_then_drains() -> None:
    s = state(0)
    sink: list[Messages] = []
    out = rate_limiter(s, 2, 60_000_000)
    out.subscribe(sink.append)
    for i in range(1, 5):
        s.down([(MessageType.DATA, i)])
    time.sleep(0.02)
    mid = _values(sink)
    assert mid == [1, 2]
    time.sleep(0.12)
    assert _values(sink) == [1, 2, 3, 4]


def test_retry_backoff_delay_rejects_non_numeric() -> None:
    src = _errors_then_ok(errors_before_ok=1)

    def bad_backoff(*_args: Any, **_kwargs: Any) -> Any:
        return "1000"

    out = retry(src, 1, backoff=bad_backoff)
    sink: list[Messages] = []
    out.subscribe(sink.append)
    time.sleep(0.05)
    assert any(m[0] is MessageType.ERROR and isinstance(m[1], TypeError) for b in sink for m in b)


def test_retry_backoff_delay_rejects_non_finite() -> None:
    src = _errors_then_ok(errors_before_ok=1)

    def bad_backoff(*_args: Any, **_kwargs: Any) -> Any:
        return float("inf")

    out = retry(src, 1, backoff=bad_backoff)
    sink: list[Messages] = []
    out.subscribe(sink.append)
    time.sleep(0.05)
    assert any(m[0] is MessageType.ERROR and isinstance(m[1], TypeError) for b in sink for m in b)


def test_circuit_breaker_opens() -> None:
    b = circuit_breaker(failure_threshold=2)
    assert b.can_execute()
    b.record_failure(ValueError("a"))
    assert b.state == "closed"
    b.record_failure(ValueError("b"))
    assert b.state == "open"
    assert not b.can_execute()


def test_with_breaker_errors_when_open() -> None:
    b = circuit_breaker(failure_threshold=1, cooldown_ns=60 * NS_PER_SEC)
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
    b = circuit_breaker(failure_threshold=1, cooldown_ns=60 * NS_PER_SEC)
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
    ad = DictCheckpointAdapter(store)
    g = Graph("x")
    g.add("n", state("hi"))
    save_graph_checkpoint(g, ad)
    assert "x" in store
    g.set("n", "bye")
    restore_graph_checkpoint(g, ad)
    assert g.get("n") == "hi"


def test_file_checkpoint_adapter_round_trip() -> None:
    with tempfile.TemporaryDirectory() as d:
        ad = FileCheckpointAdapter(d)
        g = Graph("fg")
        g.add("z", state({"k": 1}))
        save_graph_checkpoint(g, ad)
        g.set("z", {"k": 2})
        assert restore_graph_checkpoint(g, ad) is True
        assert g.get("z") == {"k": 1}
        path = Path(d) / "fg.json"
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
        # Test clear
        ad.clear("g")
        assert ad.load("g") is None
        ad.close()


def test_with_breaker_meta_integration() -> None:
    b = circuit_breaker(failure_threshold=2)
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
    b = circuit_breaker(failure_threshold=2)
    s = state(1)
    bundle = with_breaker(b)(s)
    g = Graph("test")
    g.add("src", s)
    g.add("guarded", bundle.node)
    desc = g.describe(detail="standard")
    meta_path = "guarded::__meta__::breaker_state"
    assert meta_path in desc["nodes"]
    assert desc["nodes"][meta_path]["value"] == "closed"


def test_with_status_appears_in_graph_describe() -> None:
    s = state(0)
    bundle = with_status(s)
    g = Graph("test")
    g.add("src", s)
    g.add("tracked", bundle.node)
    desc = g.describe(detail="standard")
    assert "tracked::__meta__::status" in desc["nodes"]
    assert desc["nodes"]["tracked::__meta__::status"]["value"] == "pending"
    assert "tracked::__meta__::error" in desc["nodes"]
    assert desc["nodes"]["tracked::__meta__::error"]["value"] is None


def test_restore_empty_returns_false() -> None:
    g = Graph("empty")
    g.add("a", state(0))
    assert restore_graph_checkpoint(g, MemoryCheckpointAdapter()) is False


# --- New parity tests (decorrelatedJitter, withMaxAttempts, factory, reset) ---


def test_decorrelated_jitter_basic() -> None:
    strat = decorrelated_jitter(base_ns=100_000_000, max_delay_ns=30 * NS_PER_SEC)
    d = strat(0, None, None)
    assert isinstance(d, int)
    assert d >= 100_000_000


def test_decorrelated_jitter_uses_prev_delay() -> None:
    strat = decorrelated_jitter(base_ns=100_000_000, max_delay_ns=30 * NS_PER_SEC)
    d = strat(1, None, 5 * NS_PER_SEC)
    assert d >= 100_000_000
    assert d <= 15 * NS_PER_SEC  # min(30s, 5s*3) = 15s


def test_with_max_attempts() -> None:
    inner = constant(NS_PER_SEC)
    capped = with_max_attempts(inner, 3)
    assert capped(0, None, None) == NS_PER_SEC
    assert capped(2, None, None) == NS_PER_SEC
    assert capped(3, None, None) is None  # past cap


def test_circuit_breaker_factory() -> None:
    b = circuit_breaker(failure_threshold=2)
    assert isinstance(b, CircuitBreaker)
    assert b.state == "closed"
    assert b.failure_count == 0


def test_circuit_breaker_reset() -> None:
    b = circuit_breaker(failure_threshold=1)
    b.record_failure(ValueError("x"))
    assert b.state == "open"
    b.reset()
    assert b.state == "closed"
    assert b.failure_count == 0


def test_circuit_breaker_cooldown_escalation() -> None:
    delays: list[int] = []

    def strategy(attempt: int, *_args: Any, **_kw: Any) -> int:
        d = 10_000_000 * (attempt + 1)  # 10ms * (attempt+1)
        delays.append(d)
        return d

    b = circuit_breaker(failure_threshold=1, cooldown_strategy=strategy)
    # Trip open
    b.record_failure(ValueError("a"))
    assert b.state == "open"
    time.sleep(0.02)
    # Transition to half-open
    assert b.can_execute()
    # Fail again -> open with escalated cooldown
    b.record_failure(ValueError("b"))
    assert b.state == "open"
    assert len(delays) >= 2  # strategy was called at least twice


def test_token_bucket_factory() -> None:
    tb = token_bucket(5.0, 0.0)
    assert isinstance(tb, TokenBucket)
    assert tb.try_consume(5.0)
    assert not tb.try_consume(0.1)


def test_resolve_decorrelated_jitter_preset() -> None:
    s = resolve_backoff_preset("decorrelated_jitter")
    d = s(0, None, None)
    assert isinstance(d, int)
    assert d >= 0


# --- fallback, timeout, cache ---


def test_fallback_plain_value() -> None:
    src = throw_error(ValueError("boom"))
    n = fallback(src, 42)
    sink: list[Messages] = []
    n.subscribe(sink.append)
    time.sleep(0.02)
    vals = _values(sink)
    assert 42 in vals
    assert any(m[0] is MessageType.COMPLETE for b in sink for m in b)


def test_fallback_passthrough_on_data() -> None:
    src = of(1, 2, 3)
    n = fallback(src, 99)
    sink: list[Messages] = []
    n.subscribe(sink.append)
    time.sleep(0.02)
    vals = _values(sink)
    assert vals == [1, 2, 3]


def test_fallback_node() -> None:
    src = throw_error(ValueError("boom"))
    fb_node = of(100)
    n = fallback(src, fb_node)
    sink: list[Messages] = []
    n.subscribe(sink.append)
    time.sleep(0.02)
    vals = _values(sink)
    assert 100 in vals


def test_timeout_fires() -> None:
    src = never()
    n = timeout(src, 20_000_000)  # 20ms
    sink: list[Messages] = []
    n.subscribe(sink.append)
    time.sleep(0.1)
    assert any(
        m[0] is MessageType.ERROR and isinstance(m[1], ResTimeoutError) for b in sink for m in b
    )


def test_timeout_resets_on_data() -> None:
    s = state(0)
    n = timeout(s, 80_000_000)  # 80ms
    sink: list[Messages] = []
    n.subscribe(sink.append)
    time.sleep(0.03)
    s.down([(MessageType.DATA, 1)])
    time.sleep(0.03)
    s.down([(MessageType.DATA, 2)])
    time.sleep(0.03)
    # No timeout yet - we kept resetting
    assert not any(
        m[0] is MessageType.ERROR and isinstance(m[1], ResTimeoutError) for b in sink for m in b
    )
    vals = _values(sink)
    assert 1 in vals
    assert 2 in vals


def test_timeout_cancelled_by_complete() -> None:
    src = of(1)
    n = timeout(src, 50_000_000)
    sink: list[Messages] = []
    n.subscribe(sink.append)
    time.sleep(0.1)
    # Should have completed, no timeout error
    assert any(m[0] is MessageType.COMPLETE for b in sink for m in b)
    assert not any(
        m[0] is MessageType.ERROR and isinstance(m[1], ResTimeoutError) for b in sink for m in b
    )


def test_cache_stores_and_replays() -> None:
    s = state(0)
    n = cache(s, 1_000_000_000)  # 1s TTL
    sink: list[Messages] = []
    n.subscribe(sink.append)
    s.down([(MessageType.DATA, 42)])
    time.sleep(0.02)
    vals = _values(sink)
    assert 42 in vals
    # The node retains the cached value
    assert n.get() == 42


def test_cache_forwards_live_data() -> None:
    s = state(0)
    n = cache(s, 1_000_000_000)
    sink: list[Messages] = []
    n.subscribe(sink.append)
    s.down([(MessageType.DATA, 1)])
    s.down([(MessageType.DATA, 2)])
    s.down([(MessageType.DATA, 3)])
    time.sleep(0.02)
    vals = _values(sink)
    assert vals == [1, 2, 3]


def test_timeout_range_error() -> None:
    import pytest

    with pytest.raises(ValueError, match="timeout_ns must be > 0"):
        timeout(state(0), 0)
    with pytest.raises(ValueError, match="timeout_ns must be > 0"):
        timeout(state(0), -1)


def test_cache_range_error() -> None:
    import pytest

    with pytest.raises(ValueError, match="ttl_ns must be > 0"):
        cache(state(0), 0)
    with pytest.raises(ValueError, match="ttl_ns must be > 0"):
        cache(state(0), -1)


def test_fallback_with_retry_composition() -> None:
    src = throw_error(ValueError("x"))
    n = fallback(retry(src, count=0), "safe")
    sink: list[Messages] = []
    n.subscribe(sink.append)
    time.sleep(0.05)
    vals = _values(sink)
    assert "safe" in vals
