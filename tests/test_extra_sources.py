"""Roadmap §2.3 — sources, sinks, multicast (``node`` / ``producer`` under the hood)."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import pytest

from graphrefly.core import MessageType
from graphrefly.core.node import NodeImpl, node
from graphrefly.core.sugar import producer, state
from graphrefly.extra.adapters import (
    from_event_emitter,
    from_fs_watch,
    from_git_hook,
    from_mcp,
    from_webhook,
    from_websocket,
    to_sse,
    to_websocket,
)
from graphrefly.extra.sources import (
    cached,
    empty,
    first_value_from,
    for_each,
    from_any,
    from_async_iter,
    from_awaitable,
    from_cron,
    from_iter,
    from_timer,
    never,
    of,
    replay,
    share,
    share_replay,
    throw_error,
    to_array,
    to_list,
)


def test_of_to_list() -> None:
    assert to_list(of(1, 2, 3)) == [1, 2, 3]


def test_empty_to_list() -> None:
    assert to_list(empty()) == []


def test_never_times_out() -> None:
    with pytest.raises(TimeoutError, match="timed out"):
        to_list(never(), timeout=0.05)


def test_throw_error_to_list() -> None:
    err = ValueError("x")
    with pytest.raises(ValueError, match="x"):
        to_list(throw_error(err))


def test_from_iter() -> None:
    assert to_list(from_iter([10, 20])) == [10, 20]


def test_from_iter_error() -> None:
    def bad() -> object:
        raise RuntimeError("boom")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError, match="boom"):
        to_list(from_iter(bad()))


def test_from_timer_oneshot() -> None:
    assert to_list(from_timer(0.02, None, first=7)) == [7]


def test_from_timer_periodic() -> None:
    n = from_timer(0.01, 0.05, first=0)
    out: list[Any] = []
    done = threading.Event()

    def sink(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                out.append(m[1])
            if len(out) >= 3:
                done.set()

    unsub = n.subscribe(sink)
    assert done.wait(timeout=2.0)
    unsub()
    assert out == [0, 1, 2]


def test_from_cron_subscribe_unsubscribe() -> None:
    n = from_cron("* * * * *")
    unsub = n.subscribe(lambda _m: None)
    time.sleep(0.02)
    unsub()


def test_from_awaitable() -> None:
    async def slow() -> int:
        await asyncio.sleep(0.02)
        return 99

    assert to_list(from_awaitable(slow())) == [99]


def test_from_async_iter() -> None:
    async def gen() -> object:
        yield 1
        yield 2

    assert to_list(from_async_iter(gen())) == [1, 2]


def test_from_any_node_passthrough() -> None:
    s = of(1)
    assert from_any(s) is s


def test_from_any_list() -> None:
    assert to_list(from_any([5, 6])) == [5, 6]


def test_from_any_scalar() -> None:
    assert to_list(from_any(42)) == [42]


def test_from_any_none_scalar() -> None:
    """§2.5: first DATA is always treated as changed, even when value is None."""
    sink: list[Any] = []
    n = from_any(None)
    n.subscribe(sink.append)
    # of(None) emits DATA(None) then COMPLETE — first emit is always "changed"
    assert any(m[0] is MessageType.DATA and m[1] is None for batch in sink for m in batch)
    assert any(m[0] is MessageType.COMPLETE for batch in sink for m in batch)
    assert not any(m[0] is MessageType.ERROR for batch in sink for m in batch)


def test_from_any_awaitable() -> None:
    async def make() -> int:
        return 77

    assert to_list(from_any(make())) == [77]


def test_first_value_from_and_empty() -> None:
    assert first_value_from(of(8)) == 8
    with pytest.raises(StopIteration):
        first_value_from(empty())


def test_for_each() -> None:
    acc: list[Any] = []
    u = for_each(of(1, 2), acc.append)
    time.sleep(0.05)
    u()
    assert acc == [1, 2]


def test_for_each_error_with_handler() -> None:
    """Prefer ``on_error`` for ERROR tuples (default raise may not surface to caller)."""
    seen: list[BaseException] = []

    def on_err(e: BaseException) -> None:
        seen.append(e)

    u = for_each(throw_error(ValueError("bad")), lambda _x: None, on_error=on_err)
    time.sleep(0.05)
    u()
    assert len(seen) == 1
    assert str(seen[0]) == "bad"


def test_share_multicast_wires_one_upstream() -> None:
    """``share`` is a no-fn dep wire: one subscription to the source, fan-out to many sinks."""
    root = node()  # SENTINEL: no push on subscribe
    src = share(root)
    a: list[Any] = []
    b: list[Any] = []

    def sink_a(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                a.append(m[1])

    def sink_b(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                b.append(m[1])

    ua = src.subscribe(sink_a)
    ub = src.subscribe(sink_b)
    root.down([(MessageType.DIRTY,), (MessageType.DATA, 7)])
    deadline = time.time() + 2.0
    while time.time() < deadline and (len(a) < 1 or len(b) < 1):
        time.sleep(0.01)
    ua()
    ub()
    assert a == [7]
    assert b == [7]


def test_cached_and_replay_are_nodes() -> None:
    s = of(1)
    assert isinstance(cached(s), NodeImpl)
    assert isinstance(replay(s, buffer_size=3), NodeImpl)


def test_share_replay_alias_identity() -> None:
    assert share_replay is replay


def test_replay_buffer_size_negative() -> None:
    with pytest.raises(ValueError, match="buffer_size"):
        replay(of(1), buffer_size=-1)


def test_replay_buffer_replays_to_late_subscriber() -> None:
    """Push values through replay, subscribe late, verify buffer replayed."""
    root = state(0)
    r = replay(root, buffer_size=3)

    # First subscriber to activate the chain
    first_vals: list[Any] = []

    def sink_first(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                first_vals.append(m[1])

    u1 = r.subscribe(sink_first)

    # Push values through root
    for v in [10, 20, 30, 40]:
        root.down([(MessageType.DIRTY,), (MessageType.DATA, v)])
        time.sleep(0.02)

    # Late subscriber should get last 3 buffered values
    late_vals: list[Any] = []

    def sink_late(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                late_vals.append(m[1])

    u2 = r.subscribe(sink_late)
    time.sleep(0.05)

    u1()
    u2()
    # Late subscriber should have received replayed [20, 30, 40]
    assert late_vals[:3] == [20, 30, 40]


def test_replay_buffer_size_one() -> None:
    """cached replays last value via replay(buffer_size=1) semantics."""
    root = state(0)
    r = replay(root, buffer_size=1)

    first_vals: list[Any] = []

    def sink_first(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                first_vals.append(m[1])

    u1 = r.subscribe(sink_first)

    root.down([(MessageType.DIRTY,), (MessageType.DATA, 99)])
    time.sleep(0.02)

    late_vals: list[Any] = []

    def sink_late(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                late_vals.append(m[1])

    u2 = r.subscribe(sink_late)
    time.sleep(0.05)

    u1()
    u2()
    assert late_vals[0] == 99


def test_replay_rejects_zero_buffer() -> None:
    with pytest.raises(ValueError, match="buffer_size must be >= 1"):
        replay(of(1), buffer_size=0)


def test_share_initial_value() -> None:
    s = state(42)
    shared = share(s)
    # Push-model: value flows on subscribe, not at construction time.
    unsub = shared.subscribe(lambda _m: None)
    assert shared.get() == 42
    unsub()


def test_from_cron_builtin_parser() -> None:
    """Verify from_cron works without croniter (built-in parser)."""
    n = from_cron("* * * * *")
    out: list[Any] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                out.append(m[1])

    unsub = n.subscribe(sink)
    # The cron fires immediately if current minute matches "* * * * *"
    time.sleep(0.1)
    unsub()
    # "* * * * *" matches every minute, so it should fire once on first check
    assert len(out) >= 1
    assert isinstance(out[0], int)  # time_ns returns int


def test_from_event_emitter() -> None:
    """Simple emitter class, verify DATA received."""

    class SimpleEmitter:
        def __init__(self) -> None:
            self._listeners: dict[str, list] = {}

        def add_listener(self, event: str, fn: Any) -> None:
            self._listeners.setdefault(event, []).append(fn)

        def remove_listener(self, event: str, fn: Any) -> None:
            if event in self._listeners:
                self._listeners[event] = [f for f in self._listeners[event] if f is not fn]

        def emit(self, event: str, *args: Any) -> None:
            for fn in self._listeners.get(event, []):
                fn(*args)

    emitter = SimpleEmitter()
    n = from_event_emitter(emitter, "data")
    out: list[Any] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                out.append(m[1])

    unsub = n.subscribe(sink)
    emitter.emit("data", "hello")
    emitter.emit("data", "world")
    time.sleep(0.05)
    unsub()
    assert out == ["hello", "world"]


def test_from_fs_watch_debounce_without_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    callbacks: list[Callable[[str, str, str, str | None, str | None], None]] = []

    def fake_backend(
        paths: list[str],
        recursive: bool,
        on_event: Callable[[str, str, str, str | None, str | None], None],
        on_error: Callable[[BaseException], None],
    ) -> tuple[list[Any], Callable[[], None]]:
        _ = (paths, recursive)
        callbacks.append(on_event)
        _ = on_error
        return [], lambda: None

    monkeypatch.setattr("graphrefly.extra.adapters._build_watchdog_backend", fake_backend)
    sink: list[Any] = []
    node = from_fs_watch(
        "/tmp/project",
        debounce=0.03,
        include=["**/*.py"],
        exclude=["**/ignored/**"],
    )
    unsub = node.subscribe(sink.append)
    cb = callbacks[0]
    cb("modified", "/tmp/project/main.py", "/tmp/project", None, None)
    cb("modified", "/tmp/project/main.py", "/tmp/project", None, None)
    cb("modified", "/tmp/project/ignored/nope.py", "/tmp/project", None, None)
    time.sleep(0.1)
    unsub()
    events = [m[1] for batch in sink for m in batch if m[0] is MessageType.DATA]
    assert len(events) == 1
    assert events[0]["path"].endswith("/main.py")
    assert events[0]["relative_path"] == "main.py"
    assert events[0]["root"] == "/tmp/project"
    assert isinstance(events[0]["timestamp_ns"], int)


def test_from_fs_watch_runtime_backend_error_emits_error(monkeypatch: pytest.MonkeyPatch) -> None:
    error_callbacks: list[Callable[[BaseException], None]] = []

    def fake_backend(
        paths: list[str],
        recursive: bool,
        on_event: Callable[[str, str, str, str | None, str | None], None],
        on_error: Callable[[BaseException], None],
    ) -> tuple[list[Any], Callable[[], None]]:
        _ = (paths, recursive, on_event)
        error_callbacks.append(on_error)
        return [], lambda: None

    monkeypatch.setattr("graphrefly.extra.adapters._build_watchdog_backend", fake_backend)
    sink: list[Any] = []
    node = from_fs_watch("/tmp/project", debounce=0.03)
    unsub = node.subscribe(sink.append)
    error_callbacks[0](RuntimeError("watch-failed"))
    time.sleep(0.05)
    unsub()
    assert any(
        m[0] is MessageType.ERROR and isinstance(m[1], RuntimeError) and str(m[1]) == "watch-failed"
        for batch in sink
        for m in batch
    )


def test_from_fs_watch_setup_failure_emits_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_backend(
        paths: list[str],
        recursive: bool,
        on_event: Callable[[str, str, str, str | None, str | None], None],
        on_error: Callable[[BaseException], None],
    ) -> tuple[list[Any], Callable[[], None]]:
        _ = (paths, recursive, on_event, on_error)
        raise RuntimeError("setup-failed")

    monkeypatch.setattr("graphrefly.extra.adapters._build_watchdog_backend", fake_backend)
    sink: list[Any] = []
    node = from_fs_watch("/tmp/project", debounce=0.03)
    unsub = node.subscribe(sink.append)
    time.sleep(0.05)
    unsub()
    errors = [m[1] for batch in sink for m in batch if m[0] is MessageType.ERROR]
    assert len(errors) >= 1
    err = errors[0]
    # Error may be wrapped with node name context; check cause chain
    cause = err.__cause__ if err.__cause__ is not None else err
    assert isinstance(cause, RuntimeError)
    assert str(cause) == "setup-failed"


def test_from_fs_watch_rejects_empty_paths() -> None:
    with pytest.raises(ValueError, match="at least one path"):
        from_fs_watch([])


def test_from_webhook_emits_and_cleans_up() -> None:
    emitted: list[Any] = []
    cleanup_called = [False]
    hook: dict[str, Any] = {"emit": None, "error": None, "complete": None}

    def register(
        emit: Callable[[Any], None],
        error: Callable[[BaseException | Any], None],
        complete: Callable[[], None],
    ) -> Callable[[], None]:
        hook["emit"] = emit
        hook["error"] = error
        hook["complete"] = complete

        def cleanup() -> None:
            cleanup_called[0] = True

        return cleanup

    n = from_webhook(register)

    def sink(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                emitted.append(m[1])

    unsub = n.subscribe(sink)
    hook["emit"]({"id": "evt-1"})
    time.sleep(0.02)
    unsub()
    assert emitted == [{"id": "evt-1"}]
    assert cleanup_called[0] is True


def test_from_webhook_register_error_forwards_error() -> None:
    def bad_register(
        _emit: Callable[[Any], None],
        _error: Callable[[BaseException | Any], None],
        _complete: Callable[[], None],
    ) -> Callable[[], None] | None:
        raise RuntimeError("register-failed")

    sink: list[Any] = []
    n = from_webhook(bad_register)
    n.subscribe(sink.append)
    assert any(
        m[0] is MessageType.ERROR
        and isinstance(m[1], RuntimeError)
        and str(m[1]) == "register-failed"
        for batch in sink
        for m in batch
    )


def test_from_websocket_socket_like_emits_data_and_complete() -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self._listeners: dict[str, list[Any]] = {}

        def add_listener(self, event: str, fn: Any) -> None:
            self._listeners.setdefault(event, []).append(fn)

        def remove_listener(self, event: str, fn: Any) -> None:
            if event in self._listeners:
                self._listeners[event] = [f for f in self._listeners[event] if f is not fn]

        def emit(self, event: str, *args: Any) -> None:
            for fn in self._listeners.get(event, []):
                fn(*args)

        def close(self) -> None:
            pass

    ws = FakeSocket()
    sink: list[Any] = []
    n = from_websocket(ws)
    unsub = n.subscribe(sink.append)
    ws.emit("message", {"id": "m1"})
    ws.emit("close")
    time.sleep(0.02)
    unsub()
    assert any(m[0] is MessageType.DATA and m[1] == {"id": "m1"} for batch in sink for m in batch)
    assert any(m[0] is MessageType.COMPLETE for batch in sink for m in batch)


def test_from_websocket_register_branch_emits_data_and_cleans_up() -> None:
    emit_ref: dict[str, Any] = {}
    cleaned = {"ok": False}

    def register(
        emit: Callable[[Any], None],
        _error: Callable[[BaseException | Any], None],
        complete: Callable[[], None],
    ) -> Callable[[], None]:
        emit_ref["emit"] = emit
        emit_ref["complete"] = complete

        def cleanup() -> None:
            cleaned["ok"] = True

        return cleanup

    sink: list[Any] = []
    n = from_websocket(register=register)
    unsub = n.subscribe(sink.append)
    emit_ref["emit"]({"id": "m1"})
    emit_ref["complete"]()
    time.sleep(0.02)
    unsub()
    assert any(m[0] is MessageType.DATA and m[1] == {"id": "m1"} for batch in sink for m in batch)
    assert any(m[0] is MessageType.COMPLETE for batch in sink for m in batch)
    assert cleaned["ok"] is True


def test_from_websocket_register_requires_cleanup_callable() -> None:
    def register(
        _emit: Callable[[Any], None],
        _error: Callable[[BaseException | Any], None],
        _complete: Callable[[], None],
    ) -> None:
        return None

    sink: list[Any] = []
    n = from_websocket(register=register)
    n.subscribe(sink.append)
    assert any(
        m[0] is MessageType.ERROR and "register contract violation" in str(m[1])
        for batch in sink
        for m in batch
    )


def test_from_websocket_extracts_data_field_before_parse() -> None:
    class EventObj:
        def __init__(self, data: Any) -> None:
            self.data = data

    class FakeSocket:
        def __init__(self) -> None:
            self._listeners: dict[str, list[Any]] = {}

        def add_listener(self, event: str, fn: Any) -> None:
            self._listeners.setdefault(event, []).append(fn)

        def remove_listener(self, event: str, fn: Any) -> None:
            if event in self._listeners:
                self._listeners[event] = [f for f in self._listeners[event] if f is not fn]

        def emit(self, event: str, *args: Any) -> None:
            for fn in self._listeners.get(event, []):
                fn(*args)

    ws = FakeSocket()
    sink: list[Any] = []
    n = from_websocket(ws)
    unsub = n.subscribe(sink.append)
    ws.emit("message", EventObj({"id": "obj"}))
    ws.emit("message", {"data": {"id": "dict"}})
    time.sleep(0.02)
    unsub()
    values = [m[1] for batch in sink for m in batch if m[0] is MessageType.DATA]
    assert {"id": "obj"} in values
    assert {"id": "dict"} in values


def test_from_websocket_close_detaches_listeners_immediately() -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self._listeners: dict[str, list[Any]] = {}

        def add_listener(self, event: str, fn: Any) -> None:
            self._listeners.setdefault(event, []).append(fn)

        def remove_listener(self, event: str, fn: Any) -> None:
            if event in self._listeners:
                self._listeners[event] = [f for f in self._listeners[event] if f is not fn]

        def emit(self, event: str, *args: Any) -> None:
            for fn in self._listeners.get(event, []):
                fn(*args)

    ws = FakeSocket()
    sink: list[Any] = []
    n = from_websocket(ws)
    unsub = n.subscribe(sink.append)
    ws.emit("close")
    # No explicit unsubscribe yet; listeners should already be detached.
    assert ws._listeners.get("message", []) == []
    assert ws._listeners.get("error", []) == []
    assert ws._listeners.get("close", []) == []
    unsub()


def test_from_websocket_parse_error_emits_error() -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self._listeners: dict[str, list[Any]] = {}

        def add_listener(self, event: str, fn: Any) -> None:
            self._listeners.setdefault(event, []).append(fn)

        def remove_listener(self, event: str, fn: Any) -> None:
            if event in self._listeners:
                self._listeners[event] = [f for f in self._listeners[event] if f is not fn]

        def emit(self, event: str, *args: Any) -> None:
            for fn in self._listeners.get(event, []):
                fn(*args)

    ws = FakeSocket()
    sink: list[Any] = []
    n = from_websocket(ws, parse=lambda _v: (_ for _ in ()).throw(ValueError("bad-parse")))
    unsub = n.subscribe(sink.append)
    ws.emit("message", {"id": "m1"})
    time.sleep(0.02)
    unsub()
    assert any(
        m[0] is MessageType.ERROR and isinstance(m[1], ValueError) and str(m[1]) == "bad-parse"
        for batch in sink
        for m in batch
    )


def test_from_websocket_socket_like_emits_error() -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self._listeners: dict[str, list[Any]] = {}

        def add_listener(self, event: str, fn: Any) -> None:
            self._listeners.setdefault(event, []).append(fn)

        def remove_listener(self, event: str, fn: Any) -> None:
            if event in self._listeners:
                self._listeners[event] = [f for f in self._listeners[event] if f is not fn]

        def emit(self, event: str, *args: Any) -> None:
            for fn in self._listeners.get(event, []):
                fn(*args)

        def close(self) -> None:
            pass

    ws = FakeSocket()
    sink: list[Any] = []
    n = from_websocket(ws)
    unsub = n.subscribe(sink.append)
    ws.emit("error", RuntimeError("bad"))
    time.sleep(0.02)
    unsub()
    assert any(m[0] is MessageType.ERROR for batch in sink for m in batch)


def test_from_websocket_close_on_cleanup_calls_socket_close() -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self._listeners: dict[str, list[Any]] = {}
            self.closed = 0

        def add_listener(self, event: str, fn: Any) -> None:
            self._listeners.setdefault(event, []).append(fn)

        def remove_listener(self, event: str, fn: Any) -> None:
            if event in self._listeners:
                self._listeners[event] = [f for f in self._listeners[event] if f is not fn]

        def close(self) -> None:
            self.closed += 1

    ws = FakeSocket()
    n = from_websocket(ws, close_on_cleanup=True)
    unsub = n.subscribe(lambda _batch: None)
    time.sleep(0.02)
    unsub()
    assert ws.closed == 1


def test_to_websocket_sends_data_and_closes_on_complete() -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.sent: list[Any] = []
            self.closed = 0

        def send(self, payload: Any) -> None:
            self.sent.append(payload)

        def close(self) -> None:
            self.closed += 1

    ws = FakeSocket()
    unsub = to_websocket(of(1, 2), ws, serialize=lambda v: f"n:{v}")
    time.sleep(0.02)
    unsub()
    assert ws.sent == ["n:1", "n:2"]
    assert ws.closed == 1


def test_to_websocket_passes_close_code_and_reason() -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.sent: list[Any] = []
            self.closed_args: list[tuple[Any, Any]] = []

        def send(self, payload: Any) -> None:
            self.sent.append(payload)

        def close(self, code: int | None = None, reason: str | None = None) -> None:
            self.closed_args.append((code, reason))

    ws = FakeSocket()
    unsub = to_websocket(of(1), ws, close_code=4001, close_reason="done")
    time.sleep(0.02)
    unsub()
    assert ws.sent == ["1"]
    assert ws.closed_args == [(4001, "done")]


def test_to_websocket_close_is_idempotent_for_repeated_terminals() -> None:
    n = producer(
        lambda _deps, actions: (
            actions.down([(MessageType.COMPLETE,), (MessageType.ERROR, RuntimeError("late"))]),
            (lambda: None),
        )[1]
    )

    class FakeSocket:
        def __init__(self) -> None:
            self.closed = 0

        def send(self, payload: Any) -> None:
            _ = payload

        def close(self) -> None:
            self.closed += 1

    ws = FakeSocket()
    unsub = to_websocket(n, ws)
    time.sleep(0.02)
    unsub()
    assert ws.closed == 1


def test_to_websocket_reports_structured_transport_send_error() -> None:
    errors: list[dict[str, Any]] = []

    class FakeSocket:
        def send(self, _payload: Any) -> None:
            raise RuntimeError("send-failed")

        def close(self) -> None:
            pass

    ws = FakeSocket()
    unsub = to_websocket(of(1), ws, on_transport_error=errors.append)
    time.sleep(0.02)
    unsub()
    assert len(errors) == 1
    assert errors[0]["stage"] == "send"
    assert isinstance(errors[0]["error"], RuntimeError)
    assert str(errors[0]["error"]) == "send-failed"
    assert errors[0]["message"][0] is MessageType.DATA


def test_to_websocket_reports_structured_transport_close_error() -> None:
    errors: list[dict[str, Any]] = []

    class FakeSocket:
        def send(self, _payload: Any) -> None:
            pass

        def close(self, _code: int | None = None, _reason: str | None = None) -> None:
            raise RuntimeError("close-failed")

    ws = FakeSocket()
    unsub = to_websocket(of(1), ws, on_transport_error=errors.append)
    time.sleep(0.02)
    unsub()
    assert len(errors) == 1
    assert errors[0]["stage"] == "close"
    assert isinstance(errors[0]["error"], RuntimeError)
    assert str(errors[0]["error"]) == "close-failed"
    assert errors[0]["message"][0] is MessageType.COMPLETE


def test_to_array_reactive() -> None:
    """of(1,2,3) | to_array -> Node that emits [1,2,3]."""
    result = to_list(of(1, 2, 3) | to_array)
    assert result == [[1, 2, 3]]


def test_to_sse_data_and_complete_frames() -> None:
    chunks = list(to_sse(of(1, 2)))
    text = "".join(chunks)
    assert "event: data\ndata: 1\n\n" in text
    assert "event: data\ndata: 2\n\n" in text
    assert "event: complete\n\n" in text


def test_to_sse_error_frame() -> None:
    text = "".join(
        to_sse(
            throw_error(ValueError("boom")),
            serialize=lambda v: str(v),
        )
    )
    assert "event: error\ndata: boom\n\n" in text


def test_to_sse_default_error_serialization_uses_message() -> None:
    text = "".join(to_sse(throw_error(ValueError("boom"))))
    assert "event: error\ndata: boom\n\n" in text


def test_to_sse_custom_serializer_can_override_error_payload() -> None:
    text = "".join(
        to_sse(
            throw_error(ValueError("boom")),
            serialize=lambda v: f"ERR:{type(v).__name__}",
        )
    )
    assert "event: error\ndata: ERR:ValueError\n\n" in text


def test_to_sse_keepalive_and_cancel_event() -> None:
    cancel = threading.Event()
    out: list[str] = []

    def consume() -> None:
        for chunk in to_sse(never(), keepalive_s=0.01, cancel_event=cancel):
            out.append(chunk)

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    time.sleep(0.03)
    cancel.set()
    t.join(timeout=1.0)
    assert t.is_alive() is False
    assert any(chunk == ": keepalive\n\n" for chunk in out)


def test_to_sse_can_include_dirty_and_resolved() -> None:
    n = producer(
        lambda _deps, actions: (
            actions.down([(MessageType.DIRTY,), (MessageType.RESOLVED,), (MessageType.COMPLETE,)]),
            (lambda: None),
        )[1]
    )
    text = "".join(to_sse(n, include_dirty=True, include_resolved=True))
    assert "event: DIRTY\n\n" in text
    assert "event: RESOLVED\n\n" in text


def test_to_sse_preserves_trailing_newline_lines() -> None:
    text = "".join(to_sse(of("a\n")))
    assert "event: data\ndata: a\ndata: \n\n" in text


# --- fromMCP ---


class _FakeMCPClient:
    def __init__(self) -> None:
        self._handlers: dict[str, Callable[..., None]] = {}

    def set_notification_handler(self, method: str, handler: Callable[..., None]) -> None:
        self._handlers[method] = handler

    def push(self, method: str, payload: Any) -> None:
        h = self._handlers.get(method)
        if h is not None:
            h(payload)


def _collect_data(values: list[Any]) -> Callable[[Any], None]:
    return lambda msgs: [values.append(m[1]) for m in msgs if m[0] == MessageType.DATA]


def _collect_errors(values: list[Any]) -> Callable[[Any], None]:
    return lambda msgs: [values.append(m[1]) for m in msgs if m[0] == MessageType.ERROR]


def test_from_mcp_emits_data() -> None:
    client = _FakeMCPClient()
    n = from_mcp(client, method="notifications/tools/list_changed")
    values: list[Any] = []
    unsub = n.subscribe(_collect_data(values))

    client.push("notifications/tools/list_changed", {"tools": ["a"]})
    client.push("notifications/tools/list_changed", {"tools": ["a", "b"]})

    assert len(values) == 2
    assert values[0] == {"tools": ["a"]}
    assert values[1] == {"tools": ["a", "b"]}
    unsub()


def test_from_mcp_defaults_to_notifications_message() -> None:
    client = _FakeMCPClient()
    n = from_mcp(client)
    values: list[Any] = []
    unsub = n.subscribe(_collect_data(values))

    client.push("notifications/message", "hello")
    assert values == ["hello"]
    unsub()


def test_from_mcp_suppresses_after_teardown() -> None:
    client = _FakeMCPClient()
    n = from_mcp(client)
    values: list[Any] = []
    unsub = n.subscribe(_collect_data(values))

    client.push("notifications/message", "before")
    count = len(values)
    unsub()
    client.push("notifications/message", "after")
    assert len(values) == count


def test_from_mcp_cleanup_sets_noop_handler() -> None:
    client = _FakeMCPClient()
    n = from_mcp(client)
    unsub = n.subscribe(lambda _msgs: None)

    before = client._handlers["notifications/message"]
    unsub()
    after = client._handlers["notifications/message"]

    assert before is not after
    # no-op after teardown should not raise or emit
    after({"ignored": True})


def test_from_mcp_on_disconnect_emits_error() -> None:
    client = _FakeMCPClient()
    dc_cb: list[Any] = []

    def on_disconnect(cb: Any) -> None:
        dc_cb.append(cb)

    n = from_mcp(client, on_disconnect=on_disconnect)
    errors: list[Any] = []
    unsub = n.subscribe(_collect_errors(errors))

    assert len(dc_cb) == 1
    dc_cb[0](Exception("transport closed"))

    assert len(errors) == 1
    assert str(errors[0]) == "transport closed"
    unsub()


# --- fromGitHook ---


def test_from_git_hook_emits_on_head_change(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    call_count = [0]
    sha1 = "aaa111"
    sha2 = "bbb222"

    def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
        cmd_str = " ".join(cmd)
        if "rev-parse HEAD" in cmd_str:
            call_count[0] += 1
            return type("R", (), {"stdout": sha1 if call_count[0] <= 1 else sha2})()
        if "diff --name-only" in cmd_str:
            return type("R", (), {"stdout": "src/foo.py\nsrc/bar.py\n"})()
        if "--format=%s" in cmd_str:
            return type("R", (), {"stdout": "fix: something"})()
        if "--format=%an" in cmd_str:
            return type("R", (), {"stdout": "Alice"})()
        return type("R", (), {"stdout": ""})()

    monkeypatch.setattr(subprocess, "run", fake_run)

    n = from_git_hook("/fake/repo", poll_ms=50)
    events: list[Any] = []
    unsub = n.subscribe(_collect_data(events))

    # Wait for at least one poll
    time.sleep(0.15)

    unsub()

    assert len(events) >= 1
    evt = events[0]
    assert evt["commit"] == sha2
    assert evt["files"] == ["src/foo.py", "src/bar.py"]
    assert evt["message"] == "fix: something"
    assert evt["author"] == "Alice"
    assert evt["timestamp_ns"] > 0


def test_from_git_hook_no_emit_when_head_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
        return type("R", (), {"stdout": "aaa111"})()

    monkeypatch.setattr(subprocess, "run", fake_run)

    n = from_git_hook("/fake/repo", poll_ms=50)
    events: list[Any] = []
    unsub = n.subscribe(_collect_data(events))

    time.sleep(0.15)
    unsub()

    assert len(events) == 0


def test_from_git_hook_filters_files(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    call_count = [0]

    def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
        cmd_str = " ".join(cmd)
        if "rev-parse HEAD" in cmd_str:
            call_count[0] += 1
            return type("R", (), {"stdout": "aaa" if call_count[0] <= 1 else "bbb"})()
        if "diff --name-only" in cmd_str:
            return type("R", (), {"stdout": "src/foo.py\ndocs/readme.md\ntest/bar.py\n"})()
        if "--format=%s" in cmd_str:
            return type("R", (), {"stdout": "update"})()
        if "--format=%an" in cmd_str:
            return type("R", (), {"stdout": "Bob"})()
        return type("R", (), {"stdout": ""})()

    monkeypatch.setattr(subprocess, "run", fake_run)

    n = from_git_hook("/fake/repo", poll_ms=50, include=["src/**"], exclude=["**/*.md"])
    events: list[Any] = []
    unsub = n.subscribe(_collect_data(events))

    time.sleep(0.15)
    unsub()

    assert len(events) >= 1
    assert events[0]["files"] == ["src/foo.py"]


def test_from_git_hook_emits_error_when_git_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    call_count = [0]

    def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
        cmd_str = " ".join(cmd)
        if "rev-parse HEAD" in cmd_str:
            call_count[0] += 1
            if call_count[0] <= 1:
                return type("R", (), {"stdout": "aaa111"})()
            raise subprocess.CalledProcessError(returncode=1, cmd=cmd, stderr="git failed")
        return type("R", (), {"stdout": ""})()

    monkeypatch.setattr(subprocess, "run", fake_run)

    n = from_git_hook("/fake/repo", poll_ms=50)
    errors: list[Any] = []
    unsub = n.subscribe(_collect_errors(errors))

    time.sleep(0.15)
    unsub()

    assert len(errors) == 1
    assert isinstance(errors[0], subprocess.CalledProcessError)


def test_from_git_hook_no_emissions_after_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    call_count = [0]

    def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
        cmd_str = " ".join(cmd)
        if "rev-parse HEAD" in cmd_str:
            call_count[0] += 1
            return type("R", (), {"stdout": "aaa111" if call_count[0] <= 1 else "bbb222"})()
        if "diff --name-only" in cmd_str:
            return type("R", (), {"stdout": "src/foo.py\n"})()
        if "--format=%s" in cmd_str:
            return type("R", (), {"stdout": "update"})()
        if "--format=%an" in cmd_str:
            return type("R", (), {"stdout": "Alice"})()
        return type("R", (), {"stdout": ""})()

    monkeypatch.setattr(subprocess, "run", fake_run)

    n = from_git_hook("/fake/repo", poll_ms=200)
    events: list[Any] = []
    unsub = n.subscribe(_collect_data(events))

    # Unsubscribe before first poll fires; no events should arrive afterward.
    time.sleep(0.05)
    unsub()
    time.sleep(0.3)

    assert events == []
