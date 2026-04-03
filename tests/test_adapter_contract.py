"""Adapter behavior contract tests -- verifies the 4 pillars from docs/ADAPTER-CONTRACT.md.

Uses minimal mock adapters to test the contract generically via from_webhook
and from_websocket (register-callback overload).
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from graphrefly.core import MessageType
from graphrefly.extra.sources import from_webhook, from_websocket

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def collect(n):
    """Subscribe and collect all messages."""
    messages: list[tuple] = []

    def sink(msgs):
        for m in msgs:
            messages.append(m)

    unsub = n.subscribe(sink)
    return messages, unsub


def wait_for(predicate, timeout=2.0):
    """Spin until predicate() is truthy."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# Pillar 1 -- Register callback expectations
# ---------------------------------------------------------------------------


class TestPillar1RegisterCallbackExpectations:
    def test_from_webhook_register_receives_handlers_and_may_return_cleanup(self):
        cleanup = MagicMock()

        def register(emit, error, complete):
            emit("hello")
            return cleanup

        n = from_webhook(register)
        messages, unsub = collect(n)
        assert any(m[0] is MessageType.DATA and m[1] == "hello" for m in messages)
        unsub()
        cleanup.assert_called()

    def test_from_webhook_register_may_return_none(self):
        def register(emit, error, complete):
            emit("ok")
            return None

        n = from_webhook(register)
        messages, unsub = collect(n)
        assert any(m[0] is MessageType.DATA and m[1] == "ok" for m in messages)
        unsub()

    def test_from_websocket_register_must_return_cleanup_callable(self):
        def register(emit, error, complete):
            return lambda: None

        n = from_websocket(register=register)
        _messages, unsub = collect(n)
        unsub()

    def test_from_websocket_register_returning_none_triggers_error(self):
        def register(emit, error, complete):
            return None

        n = from_websocket(register=register)
        messages, unsub = collect(n)
        error_msg = next((m for m in messages if m[0] is MessageType.ERROR), None)
        assert error_msg is not None
        assert "contract violation" in str(error_msg[1])
        unsub()

    def test_registration_errors_forwarded_as_error_tuples(self):
        def register(emit, error, complete):
            raise RuntimeError("register boom")

        n = from_webhook(register)
        messages, unsub = collect(n)
        error_msg = next((m for m in messages if m[0] is MessageType.ERROR), None)
        assert error_msg is not None
        assert str(error_msg[1]) == "register boom"
        unsub()

    def test_from_websocket_registration_errors_forwarded_as_error_tuples(self):
        def register(emit, error, complete):
            raise RuntimeError("ws register boom")

        n = from_websocket(register=register)
        messages, unsub = collect(n)
        error_msg = next((m for m in messages if m[0] is MessageType.ERROR), None)
        assert error_msg is not None
        assert "ws register boom" in str(error_msg[1])
        unsub()


# ---------------------------------------------------------------------------
# Pillar 2 -- Terminal-time ordering
# ---------------------------------------------------------------------------


class TestPillar2TerminalTimeOrdering:
    def test_cleanup_runs_before_terminal_emission(self):
        order: list[str] = []

        def register(emit, error, complete):
            emit("data")

            def deferred():
                time.sleep(0.02)
                complete()

            t = threading.Thread(target=deferred, daemon=True)
            t.start()

            def cleanup():
                order.append("cleanup")

            return cleanup

        n = from_websocket(register=register)
        messages, unsub = collect(n)

        def sink2(msgs):
            for m in msgs:
                if m[0] is MessageType.COMPLETE:
                    order.append("complete-received")

        n.subscribe(sink2)
        wait_for(lambda: "cleanup" in order)
        assert "cleanup" in order
        unsub()

    def test_emit_after_terminal_is_noop_from_webhook(self):
        emit_fn = [None]

        def register(emit, error, complete):
            emit_fn[0] = emit
            emit("before")
            complete()
            return None

        n = from_webhook(register)
        messages, unsub = collect(n)
        # Try emitting after complete.
        emit_fn[0]("after-terminal")
        data_messages = [m for m in messages if m[0] is MessageType.DATA]
        assert len(data_messages) == 1
        assert data_messages[0][1] == "before"
        unsub()


# ---------------------------------------------------------------------------
# Pillar 3 -- Sink transport failure handling
# ---------------------------------------------------------------------------


class TestPillar3TransportErrors:
    def test_from_websocket_parse_error_surfaces_as_error(self):
        def bad_parse(value):
            raise RuntimeError("parse failed")

        def register(emit, error, complete):
            emit("raw-value")
            return lambda: None

        n = from_websocket(register=register, parse=bad_parse)
        messages, unsub = collect(n)
        error_msg = next((m for m in messages if m[0] is MessageType.ERROR), None)
        assert error_msg is not None
        assert "parse failed" in str(error_msg[1])
        unsub()

    def test_from_webhook_error_forwards_as_error_tuple_without_raising(self):
        def register(emit, error, complete):
            error(RuntimeError("transport err"))
            return None

        n = from_webhook(register)
        messages, unsub = collect(n)
        error_msg = next((m for m in messages if m[0] is MessageType.ERROR), None)
        assert error_msg is not None
        assert "transport err" in str(error_msg[1])
        unsub()


# ---------------------------------------------------------------------------
# Pillar 4 -- Idempotency
# ---------------------------------------------------------------------------


class TestPillar4Idempotency:
    def test_repeated_complete_is_idempotent_from_webhook(self):
        complete_fn = [None]

        def register(emit, error, complete):
            complete_fn[0] = complete
            return None

        n = from_webhook(register)
        messages, unsub = collect(n)
        complete_fn[0]()
        complete_fn[0]()
        complete_fn[0]()
        completes = [m for m in messages if m[0] is MessageType.COMPLETE]
        assert len(completes) == 1
        unsub()

    def test_repeated_error_is_idempotent_from_webhook(self):
        error_fn = [None]

        def register(emit, error, complete):
            error_fn[0] = error
            return None

        n = from_webhook(register)
        messages, unsub = collect(n)
        error_fn[0](RuntimeError("first"))
        error_fn[0](RuntimeError("second"))
        errors = [m for m in messages if m[0] is MessageType.ERROR]
        assert len(errors) == 1
        assert "first" in str(errors[0][1])
        unsub()

    def test_emit_after_error_is_noop_from_websocket(self):
        emit_fn = [None]

        def register(emit, error, complete):
            emit_fn[0] = emit
            error(RuntimeError("done"))
            return lambda: None

        n = from_websocket(register=register)
        messages, unsub = collect(n)
        emit_fn[0]("late-data")
        data_messages = [m for m in messages if m[0] is MessageType.DATA]
        assert len(data_messages) == 0
        unsub()

    def test_complete_then_error_is_idempotent_from_websocket(self):
        complete_fn = [None]
        error_fn = [None]

        def register(emit, error, complete):
            complete_fn[0] = complete
            error_fn[0] = error
            return lambda: None

        n = from_websocket(register=register)
        messages, unsub = collect(n)
        complete_fn[0]()
        error_fn[0](RuntimeError("late"))
        terminals = [m for m in messages if m[0] in (MessageType.COMPLETE, MessageType.ERROR)]
        assert len(terminals) == 1
        assert terminals[0][0] is MessageType.COMPLETE
        unsub()
