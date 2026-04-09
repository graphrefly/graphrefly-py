"""Tests for 5.3b ingest adapters (src/graphrefly/extra/adapters.py)."""

from __future__ import annotations

import json
import threading
import time
import unittest.mock
from typing import Any

import pytest

from graphrefly.core import MessageType
from graphrefly.extra.adapters import (
    from_clickhouse_watch,
    from_csv,
    from_kafka,
    from_nats,
    from_ndjson,
    from_otel,
    from_prometheus,
    from_pulsar,
    from_rabbitmq,
    from_redis_stream,
    from_statsd,
    from_syslog,
    parse_prometheus_text,
    parse_statsd,
    parse_syslog,
    to_kafka,
    to_nats,
    to_pulsar,
    to_rabbitmq,
    to_redis_stream,
)
from graphrefly.extra.sources import first_value_from, first_where, from_iter, to_list


# ——————————————————————————————————————————————————————————————
#  from_otel
# ——————————————————————————————————————————————————————————————


class TestFromOTel:
    def test_emits_traces_metrics_logs(self) -> None:
        traces: list[Any] = []
        metrics: list[Any] = []
        logs: list[Any] = []

        fire: dict[str, Any] = {}

        def register(handlers: dict[str, Any]) -> None:
            fire.update(handlers)
            return None

        bundle = from_otel(register)

        bundle.traces.subscribe(
            lambda msgs: [traces.append(m[1]) for m in msgs if m[0] is MessageType.DATA]
        )
        bundle.metrics.subscribe(
            lambda msgs: [metrics.append(m[1]) for m in msgs if m[0] is MessageType.DATA]
        )
        bundle.logs.subscribe(
            lambda msgs: [logs.append(m[1]) for m in msgs if m[0] is MessageType.DATA]
        )

        fire["on_traces"](
            [
                {
                    "trace_id": "abc",
                    "span_id": "123",
                    "operation_name": "GET /api",
                    "service_name": "web",
                    "status": "OK",
                }
            ]
        )
        fire["on_metrics"]([{"name": "http_requests_total", "value": 42}])
        fire["on_logs"]([{"body": "server started", "severity": "INFO"}])

        assert len(traces) == 1
        assert traces[0]["trace_id"] == "abc"
        assert len(metrics) == 1
        assert metrics[0]["name"] == "http_requests_total"
        assert len(logs) == 1
        assert logs[0]["body"] == "server started"

    def test_error_propagates_to_all_signals(self) -> None:
        errors: list[list[Any]] = [[], [], []]
        fire: dict[str, Any] = {}

        bundle = from_otel(lambda h: (fire.update(h), None)[1])

        for i, n in enumerate([bundle.traces, bundle.metrics, bundle.logs]):
            idx = i
            n.subscribe(
                lambda msgs, _idx=idx: [
                    errors[_idx].append(m[1]) for m in msgs if m[0] is MessageType.ERROR
                ]
            )

        fire["on_error"](RuntimeError("otel down"))

        for err_list in errors:
            assert len(err_list) == 1
            assert isinstance(err_list[0], RuntimeError)


# ——————————————————————————————————————————————————————————————
#  parse_syslog
# ——————————————————————————————————————————————————————————————


class TestParseSyslog:
    def test_rfc5424_valid(self) -> None:
        raw = "<34>1 2023-01-01T00:00:00Z host app 1234 ID001 This is a test"
        result = parse_syslog(raw)
        assert result.facility == 4  # 34 >> 3
        assert result.severity == 2  # 34 & 7
        assert result.hostname == "host"
        assert result.app_name == "app"
        assert result.proc_id == "1234"
        assert result.msg_id == "ID001"
        assert result.message == "This is a test"
        assert result.timestamp_ns > 0

    def test_unparseable_fallback(self) -> None:
        raw = "some random log line"
        result = parse_syslog(raw)
        assert result.facility == 1
        assert result.severity == 6
        assert result.message == "some random log line"
        assert result.hostname == "-"


# ——————————————————————————————————————————————————————————————
#  parse_statsd
# ——————————————————————————————————————————————————————————————


class TestParseStatsD:
    def test_counter(self) -> None:
        result = parse_statsd("page.views:1|c")
        assert result.name == "page.views"
        assert result.value == 1.0
        assert result.type == "counter"

    def test_gauge(self) -> None:
        result = parse_statsd("cpu.usage:72.5|g")
        assert result.name == "cpu.usage"
        assert result.value == 72.5
        assert result.type == "gauge"

    def test_timer(self) -> None:
        result = parse_statsd("request.latency:320|ms")
        assert result.type == "timer"
        assert result.value == 320.0

    def test_histogram(self) -> None:
        result = parse_statsd("request.size:1024|h")
        assert result.type == "histogram"

    def test_sample_rate(self) -> None:
        result = parse_statsd("page.views:1|c|@0.1")
        assert result.sample_rate == 0.1

    def test_tags(self) -> None:
        result = parse_statsd("page.views:1|c|#env:prod,region:us")
        assert result.tags == {"env": "prod", "region": "us"}

    def test_invalid_line(self) -> None:
        with pytest.raises(ValueError, match="Invalid StatsD"):
            parse_statsd("invalid")


# ——————————————————————————————————————————————————————————————
#  parse_prometheus_text
# ——————————————————————————————————————————————————————————————


class TestParsePrometheusText:
    def test_exposition_format(self) -> None:
        text = """\
# HELP http_requests_total Total HTTP requests
# TYPE http_requests_total counter
http_requests_total{method="GET",code="200"} 1027 1395066363000
http_requests_total{method="POST",code="200"} 42

# HELP process_cpu_seconds CPU time
# TYPE process_cpu_seconds gauge
process_cpu_seconds 0.44
"""
        metrics = parse_prometheus_text(text)
        assert len(metrics) == 3

        m0 = metrics[0]
        assert m0.name == "http_requests_total"
        assert m0.labels == {"method": "GET", "code": "200"}
        assert m0.value == 1027.0
        assert m0.timestamp_ms == 1395066363000.0
        assert m0.type == "counter"
        assert m0.help == "Total HTTP requests"

        m1 = metrics[1]
        assert m1.name == "http_requests_total"
        assert m1.labels == {"method": "POST", "code": "200"}
        assert m1.value == 42.0
        assert m1.timestamp_ms is None

        m2 = metrics[2]
        assert m2.name == "process_cpu_seconds"
        assert m2.labels == {}
        assert m2.value == 0.44
        assert m2.type == "gauge"

    def test_blank_and_comment_lines(self) -> None:
        text = """\
# This is a comment
# TYPE foo gauge

foo 1
"""
        metrics = parse_prometheus_text(text)
        assert len(metrics) == 1
        assert metrics[0].name == "foo"


# ——————————————————————————————————————————————————————————————
#  from_prometheus
# ——————————————————————————————————————————————————————————————


class TestFromPrometheus:
    def test_scrape_emits_metrics(self) -> None:
        prom_text = b"""\
# TYPE up gauge
up 1
"""
        received: list[Any] = []
        done = threading.Event()

        with unittest.mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = unittest.mock.MagicMock()
            mock_response.read.return_value = prom_text
            mock_response.__enter__.return_value = mock_response
            mock_urlopen.return_value = mock_response

            n = from_prometheus("http://localhost:9090/metrics", interval_ns=999_000_000_000)

            def on_msgs(msgs: Any) -> None:
                for m in msgs:
                    if m[0] is MessageType.DATA:
                        received.append(m[1])
                        done.set()

            unsub = n.subscribe(on_msgs)

            done.wait(timeout=5.0)
            unsub()

        assert len(received) >= 1
        assert received[0].name == "up"
        assert received[0].value == 1.0


# ——————————————————————————————————————————————————————————————
#  from_kafka / to_kafka
# ——————————————————————————————————————————————————————————————


class TestFromKafka:
    def test_consumer_emits_messages(self) -> None:
        received: list[Any] = []
        done = threading.Event()

        class MockConsumer:
            def subscribe(self, topics: list[str]) -> None:
                pass

            def run(self, callback: Any) -> None:
                callback(
                    topic="events",
                    partition=0,
                    key=b"key1",
                    value=b'{"action":"click"}',
                    headers={},
                    offset="0",
                    timestamp="123456",
                )
                done.set()

        consumer = MockConsumer()
        n = from_kafka(consumer, "events")

        unsub = n.subscribe(
            lambda msgs: [received.append(m[1]) for m in msgs if m[0] is MessageType.DATA]
        )

        done.wait(timeout=5.0)
        unsub()

        assert len(received) >= 1
        assert received[0]["topic"] == "events"
        assert received[0]["value"] == {"action": "click"}


class TestToKafka:
    def test_producer_send_called(self) -> None:
        sent: list[Any] = []

        class MockProducer:
            def send(self, topic: str, *, key: Any = None, value: Any = None) -> None:
                sent.append({"topic": topic, "key": key, "value": value})

        source = from_iter([1, 2, 3])
        producer = MockProducer()
        handle = to_kafka(source, producer, "output")

        handle.dispose()

        assert len(sent) == 3
        assert sent[0]["topic"] == "output"
        assert json.loads(sent[0]["value"]) == 1


# ——————————————————————————————————————————————————————————————
#  from_redis_stream / to_redis_stream
# ——————————————————————————————————————————————————————————————


class TestFromRedisStream:
    def test_xread_emits_entries(self) -> None:
        received: list[Any] = []
        call_count = [0]

        class MockRedis:
            def xread(self, streams: dict[str, str], **kwargs: Any) -> Any:
                call_count[0] += 1
                if call_count[0] == 1:
                    return [
                        (
                            "mystream",
                            [
                                ("1-0", {"data": '{"event":"login"}'}),
                                ("2-0", {"data": '{"event":"logout"}'}),
                            ],
                        )
                    ]
                # Block forever on subsequent calls to stop the loop.
                time.sleep(10)
                return None

        client = MockRedis()
        n = from_redis_stream(client, "mystream", start_id="0")

        unsub = n.subscribe(
            lambda msgs: [received.append(m[1]) for m in msgs if m[0] is MessageType.DATA]
        )

        first_where(n, lambda v: v is not None and v["id"] == "2-0", timeout=5.0)
        unsub()

        assert len(received) >= 2
        assert received[0]["id"] == "1-0"
        assert received[0]["data"] == {"event": "login"}
        assert received[1]["id"] == "2-0"


class TestToRedisStream:
    def test_xadd_called(self) -> None:
        added: list[Any] = []

        class MockRedis:
            def xadd(self, name: str, fields: dict[str, str], **kwargs: Any) -> str:
                added.append({"name": name, "fields": fields})
                return "1-0"

        source = from_iter(["hello", "world"])
        client = MockRedis()
        handle = to_redis_stream(source, client, "mystream")

        handle.dispose()

        assert len(added) == 2
        assert added[0]["name"] == "mystream"
        assert json.loads(added[0]["fields"]["data"]) == "hello"


# ——————————————————————————————————————————————————————————————
#  from_csv
# ——————————————————————————————————————————————————————————————


class TestFromCSV:
    def test_header_and_rows(self) -> None:
        lines = ["name,age,city\n", "Alice,30,NYC\n", "Bob,25,LA\n"]
        n = from_csv(iter(lines))
        result = to_list(n, timeout=5.0)
        assert len(result) == 2
        assert result[0] == {"name": "Alice", "age": "30", "city": "NYC"}
        assert result[1] == {"name": "Bob", "age": "25", "city": "LA"}

    def test_explicit_columns(self) -> None:
        lines = ["a,b\n", "1,2\n"]
        n = from_csv(iter(lines), columns=["x", "y"], has_header=False)
        result = to_list(n, timeout=5.0)
        # With explicit columns and no header, first row is data
        assert len(result) == 2
        assert result[0] == {"x": "a", "y": "b"}
        assert result[1] == {"x": "1", "y": "2"}

    def test_completes(self) -> None:
        lines = ["h\n", "v\n"]
        n = from_csv(iter(lines))
        result = to_list(n, timeout=5.0)
        assert result == [{"h": "v"}]


# ——————————————————————————————————————————————————————————————
#  from_ndjson
# ——————————————————————————————————————————————————————————————


class TestFromNDJSON:
    def test_parsed_objects(self) -> None:
        lines = ['{"a":1}\n', '{"b":2}\n', '{"c":3}\n']
        n = from_ndjson(iter(lines))
        result = to_list(n, timeout=5.0)
        assert result == [{"a": 1}, {"b": 2}, {"c": 3}]

    def test_completes(self) -> None:
        lines = ['{"x":1}\n']
        n = from_ndjson(iter(lines))
        result = to_list(n, timeout=5.0)
        assert result == [{"x": 1}]

    def test_error_on_malformed(self) -> None:
        lines = ['{"a":1}\n', "not json\n"]
        n = from_ndjson(iter(lines))
        with pytest.raises(json.JSONDecodeError):
            to_list(n, timeout=5.0)


# ——————————————————————————————————————————————————————————————
#  from_clickhouse_watch
# ——————————————————————————————————————————————————————————————


class TestFromClickHouseWatch:
    def test_rows_emitted(self) -> None:
        received: list[Any] = []
        done = threading.Event()

        class MockClient:
            def query(self, sql: str, **kwargs: Any) -> list[dict[str, Any]]:
                return [{"id": 1, "error": "timeout"}, {"id": 2, "error": "500"}]

        client = MockClient()
        n = from_clickhouse_watch(client, "SELECT * FROM errors_mv", interval_ns=999_000_000_000)

        def on_msgs(msgs: Any) -> None:
            for m in msgs:
                if m[0] is MessageType.DATA:
                    received.append(m[1])
            if len(received) >= 2:
                done.set()

        unsub = n.subscribe(on_msgs)

        done.wait(timeout=5.0)
        unsub()

        assert len(received) >= 2
        assert received[0] == {"id": 1, "error": "timeout"}
        assert received[1] == {"id": 2, "error": "500"}


# ——————————————————————————————————————————————————————————————
#  from_syslog
# ——————————————————————————————————————————————————————————————


class TestFromSyslog:
    def test_register_pattern(self) -> None:
        received: list[Any] = []
        fire: dict[str, Any] = {}

        def register(emit: Any, error: Any, complete: Any) -> None:
            fire["emit"] = emit
            fire["error"] = error
            fire["complete"] = complete
            return None

        n = from_syslog(register)
        unsub = n.subscribe(
            lambda msgs: [received.append(m[1]) for m in msgs if m[0] is MessageType.DATA]
        )

        parsed = parse_syslog("<34>1 2023-01-01T00:00:00Z host app 1234 ID001 test msg")
        fire["emit"](parsed)
        unsub()

        assert received[0].facility == 4
        assert received[0].message == "test msg"


# ——————————————————————————————————————————————————————————————
#  from_statsd
# ——————————————————————————————————————————————————————————————


class TestFromStatsD:
    def test_register_pattern(self) -> None:
        received: list[Any] = []
        fire: dict[str, Any] = {}

        def register(emit: Any, error: Any, complete: Any) -> None:
            fire["emit"] = emit
            return None

        n = from_statsd(register)
        unsub = n.subscribe(
            lambda msgs: [received.append(m[1]) for m in msgs if m[0] is MessageType.DATA]
        )

        fire["emit"](parse_statsd("req.count:1|c|#env:prod"))
        unsub()

        assert received[0].name == "req.count"
        assert received[0].tags == {"env": "prod"}


# ——————————————————————————————————————————————————————————————
#  from_pulsar / to_pulsar
# ——————————————————————————————————————————————————————————————


class TestFromPulsar:
    def test_consumer_emits_messages(self) -> None:
        received: list[Any] = []
        done = threading.Event()
        call_count = [0]

        class MockMsgId:
            def __str__(self) -> str:
                return "msg-1"

        class MockMsg:
            def topic_name(self) -> str:
                return "persistent://public/default/events"

            def message_id(self) -> Any:
                return MockMsgId()

            def partition_key(self) -> str:
                return "key1"

            def data(self) -> bytes:
                return b'{"action":"click"}'

            def properties(self) -> dict[str, str]:
                return {"source": "web"}

            def publish_timestamp(self) -> int:
                return 1704067200000

            def event_timestamp(self) -> int:
                return 1704067200001

        class MockConsumer:
            def receive(self) -> Any:
                call_count[0] += 1
                if call_count[0] == 1:
                    return MockMsg()
                done.set()
                # Block forever on second call.
                threading.Event().wait()
                return None  # Never reached.

            def acknowledge(self, msg: Any) -> None:
                pass

        consumer = MockConsumer()
        n = from_pulsar(consumer)

        unsub = n.subscribe(
            lambda msgs: [received.append(m[1]) for m in msgs if m[0] is MessageType.DATA]
        )

        done.wait(timeout=5.0)
        unsub()

        assert len(received) >= 1
        assert received[0].topic == "persistent://public/default/events"
        assert received[0].message_id == "msg-1"
        assert received[0].key == "key1"
        assert received[0].value == {"action": "click"}
        assert received[0].publish_time == 1704067200000
        assert received[0].event_time == 1704067200001

    def test_error_on_receive_failure(self) -> None:
        errors: list[Any] = []

        class MockConsumer:
            def receive(self) -> Any:
                raise RuntimeError("broker down")

            def acknowledge(self, msg: Any) -> None:
                pass

        n = from_pulsar(MockConsumer())
        unsub = n.subscribe(
            lambda msgs: [errors.append(m[1]) for m in msgs if m[0] is MessageType.ERROR]
        )

        with pytest.raises(RuntimeError, match="broker down"):
            first_value_from(n, timeout=5.0)
        unsub()

        assert len(errors) == 1
        assert str(errors[0]) == "broker down"

    def test_skips_ack_when_auto_ack_false(self) -> None:
        acked: list[bool] = []
        done = threading.Event()
        call_count = [0]

        class MockMsg:
            def topic_name(self) -> str:
                return "topic"

            def message_id(self) -> str:
                return "msg-1"

            def partition_key(self) -> str:
                return ""

            def data(self) -> bytes:
                return b'"hello"'

            def properties(self) -> dict[str, str]:
                return {}

            def publish_timestamp(self) -> int:
                return 0

            def event_timestamp(self) -> int:
                return 0

        class MockConsumer:
            def receive(self) -> Any:
                call_count[0] += 1
                if call_count[0] == 1:
                    return MockMsg()
                done.set()
                threading.Event().wait()
                return None

            def acknowledge(self, msg: Any) -> None:
                acked.append(True)

        n = from_pulsar(MockConsumer(), auto_ack=False)
        unsub = n.subscribe(lambda _msgs: None)

        done.wait(timeout=5.0)
        unsub()

        assert len(acked) == 0


class TestToPulsar:
    def test_producer_send_called(self) -> None:
        sent: list[Any] = []

        class MockProducer:
            def send(self, data: bytes, **kwargs: Any) -> None:
                sent.append({"data": data, **kwargs})

        source = from_iter([1, 2])
        handle = to_pulsar(source, MockProducer())

        handle.dispose()

        assert len(sent) == 2
        assert json.loads(sent[0]["data"]) == 1


# ——————————————————————————————————————————————————————————————
#  from_nats / to_nats
# ——————————————————————————————————————————————————————————————


class TestFromNATS:
    def test_subscription_emits_messages(self) -> None:
        received: list[Any] = []
        done = threading.Event()

        class MockMsg:
            subject = "events.click"
            data = b'{"action":"click"}'
            headers = None
            reply = "reply.1"
            sid = 1

        class MockClient:
            def subscribe(self, subject: str, **kwargs: Any) -> Any:
                def _iter() -> Any:
                    yield MockMsg()
                    done.set()

                return _iter()

            def publish(self, subject: str, data: bytes, **kwargs: Any) -> None:
                pass

        n = from_nats(MockClient(), "events.>")
        unsub = n.subscribe(
            lambda msgs: [received.append(m[1]) for m in msgs if m[0] is MessageType.DATA]
        )

        done.wait(timeout=5.0)
        unsub()

        assert len(received) >= 1
        assert received[0].subject == "events.click"
        assert received[0].data == {"action": "click"}
        assert received[0].reply == "reply.1"

    def test_complete_on_subscription_end(self) -> None:
        completed: list[bool] = []

        class MockClient:
            def subscribe(self, subject: str, **kwargs: Any) -> Any:
                return iter([])  # Empty iterator — immediate end.

            def publish(self, subject: str, data: bytes, **kwargs: Any) -> None:
                pass

        n = from_nats(MockClient(), "test")
        unsub = n.subscribe(
            lambda msgs: [completed.append(True) for m in msgs if m[0] is MessageType.COMPLETE]
        )

        # Terminal event: COMPLETE without DATA. first_value_from raises
        # StopIteration, which proves the source completed reactively.
        from graphrefly.extra.sources import first_value_from

        with pytest.raises(StopIteration):
            first_value_from(n, timeout=5.0)
        unsub()

        assert len(completed) >= 1

    def test_error_on_iteration_failure(self) -> None:
        errors: list[Any] = []

        class FailingIter:
            def __iter__(self) -> Any:
                return self

            def __next__(self) -> Any:
                raise RuntimeError("connection lost")

        class MockClient:
            def subscribe(self, subject: str, **kwargs: Any) -> Any:
                return FailingIter()

            def publish(self, subject: str, data: bytes, **kwargs: Any) -> None:
                pass

        n = from_nats(MockClient(), "test")
        unsub = n.subscribe(
            lambda msgs: [errors.append(m[1]) for m in msgs if m[0] is MessageType.ERROR]
        )

        with pytest.raises(RuntimeError, match="connection lost"):
            first_value_from(n, timeout=5.0)
        unsub()

    def test_async_subscription_emits_messages(self) -> None:
        """Async nats-py v2+ client: subscribe() returns an AsyncIterable."""
        received: list[Any] = []

        class MockMsg:
            subject = "events.click"
            data = b'{"action":"click"}'
            headers = None
            reply = "reply.1"
            sid = 1

        class MockAsyncSub:
            """Mimics nats-py v2 Subscription (AsyncIterable)."""

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                if not hasattr(self, "_sent"):
                    self._sent = True
                    return MockMsg()
                raise StopAsyncIteration

        class MockAsyncClient:
            def subscribe(self, subject: str, **kwargs: Any) -> Any:
                return MockAsyncSub()

            def publish(self, subject: str, data: bytes, **kwargs: Any) -> None:
                pass

        n = from_nats(MockAsyncClient(), "events.>")
        unsub = n.subscribe(
            lambda msgs: [received.append(m[1]) for m in msgs if m[0] is MessageType.DATA]
        )

        first_where(n, lambda v: v is not None, timeout=5.0)
        unsub()

        assert len(received) >= 1
        assert received[0].subject == "events.click"
        assert received[0].data == {"action": "click"}

    def test_async_coroutine_subscribe(self) -> None:
        """Async nats-py v2+ client: subscribe() returns a coroutine."""
        received: list[Any] = []

        class MockMsg:
            subject = "events.test"
            data = b'"hello"'
            headers = None
            reply = None
            sid = 2

        class MockAsyncSub:
            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                if not hasattr(self, "_sent"):
                    self._sent = True
                    return MockMsg()
                raise StopAsyncIteration

        class MockAsyncClient:
            def subscribe(self, subject: str, **kwargs: Any) -> Any:
                # Returns a coroutine (like nats-py v2 `await nc.subscribe(...)`)
                async def _coro() -> Any:
                    return MockAsyncSub()

                return _coro()

            def publish(self, subject: str, data: bytes, **kwargs: Any) -> None:
                pass

        n = from_nats(MockAsyncClient(), "events.test")
        unsub = n.subscribe(
            lambda msgs: [received.append(m[1]) for m in msgs if m[0] is MessageType.DATA]
        )

        first_where(n, lambda v: v is not None, timeout=5.0)
        unsub()

        assert len(received) >= 1
        assert received[0].data == "hello"

    def test_async_complete_on_subscription_end(self) -> None:
        """Async path: COMPLETE emitted when async iterator exhausts."""
        completed: list[bool] = []

        class MockEmptyAsyncSub:
            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                raise StopAsyncIteration

        class MockAsyncClient:
            def subscribe(self, subject: str, **kwargs: Any) -> Any:
                return MockEmptyAsyncSub()

            def publish(self, subject: str, data: bytes, **kwargs: Any) -> None:
                pass

        n = from_nats(MockAsyncClient(), "test")
        unsub = n.subscribe(
            lambda msgs: [completed.append(True) for m in msgs if m[0] is MessageType.COMPLETE]
        )

        with pytest.raises(StopIteration):
            first_value_from(n, timeout=5.0)
        unsub()

        assert len(completed) >= 1


class TestToNATS:
    def test_publish_called(self) -> None:
        published: list[Any] = []

        class MockClient:
            def subscribe(self, subject: str, **kwargs: Any) -> Any:
                return iter([])

            def publish(self, subject: str, data: bytes, **kwargs: Any) -> None:
                published.append({"subject": subject, "data": data})

        source = from_iter(["hello"])
        handle = to_nats(source, MockClient(), "events.out")

        handle.dispose()

        assert len(published) == 1
        assert published[0]["subject"] == "events.out"
        assert json.loads(published[0]["data"]) == "hello"


# ——————————————————————————————————————————————————————————————
#  from_rabbitmq / to_rabbitmq
# ——————————————————————————————————————————————————————————————


class TestFromRabbitMQ:
    def test_channel_emits_messages(self) -> None:
        received: list[Any] = []
        done = threading.Event()

        class MockMethod:
            routing_key = "events"
            exchange = ""
            delivery_tag = 1
            redelivered = False

        class MockProperties:
            content_type = "application/json"

        class MockChannel:
            def basic_consume(self, *, queue: str, on_message_callback: Any, auto_ack: bool) -> str:
                # Deliver one message synchronously.
                on_message_callback(self, MockMethod(), MockProperties(), b'{"action":"click"}')
                done.set()
                return "ctag-1"

            def start_consuming(self) -> None:
                # Block until teardown.
                threading.Event().wait(timeout=5.0)

            def basic_ack(self, *, delivery_tag: int) -> None:
                pass

            def basic_cancel(self, tag: str) -> None:
                pass

        n = from_rabbitmq(MockChannel(), "events")
        unsub = n.subscribe(
            lambda msgs: [received.append(m[1]) for m in msgs if m[0] is MessageType.DATA]
        )

        done.wait(timeout=5.0)
        unsub()

        assert len(received) >= 1
        assert received[0].queue == "events"
        assert received[0].routing_key == "events"
        assert received[0].content == {"action": "click"}
        assert received[0].delivery_tag == 1
        assert received[0].redelivered is False

    def test_error_on_consume_failure(self) -> None:
        errors: list[Any] = []

        class MockChannel:
            def basic_consume(self, **kwargs: Any) -> str:
                raise RuntimeError("channel closed")

            def start_consuming(self) -> None:
                pass

            def basic_ack(self, **kwargs: Any) -> None:
                pass

            def basic_cancel(self, tag: str) -> None:
                pass

        n = from_rabbitmq(MockChannel(), "events")
        unsub = n.subscribe(
            lambda msgs: [errors.append(m[1]) for m in msgs if m[0] is MessageType.ERROR]
        )

        with pytest.raises(RuntimeError, match="channel closed"):
            first_value_from(n, timeout=5.0)
        unsub()

        assert len(errors) == 1
        assert str(errors[0]) == "channel closed"

    def test_broker_cancel_emits_error(self) -> None:
        errors: list[Any] = []
        done = threading.Event()

        class MockChannel:
            def basic_consume(self, *, queue: str, on_message_callback: Any, auto_ack: bool) -> str:
                # Simulate broker cancellation: deliver None method.
                on_message_callback(self, None, None, b"")
                done.set()
                return "ctag-1"

            def start_consuming(self) -> None:
                threading.Event().wait(timeout=5.0)

            def stop_consuming(self) -> None:
                pass

            def basic_ack(self, **kwargs: Any) -> None:
                pass

            def basic_cancel(self, tag: str) -> None:
                pass

        n = from_rabbitmq(MockChannel(), "events")
        unsub = n.subscribe(
            lambda msgs: [errors.append(m[1]) for m in msgs if m[0] is MessageType.ERROR]
        )

        done.wait(timeout=5.0)
        unsub()

        assert len(errors) == 1
        assert str(errors[0]) == "Consumer cancelled by broker"


class TestToRabbitMQ:
    def test_basic_publish_called(self) -> None:
        published: list[Any] = []

        class MockChannel:
            def basic_publish(self, *, exchange: str, routing_key: str, body: bytes) -> None:
                published.append({"exchange": exchange, "routing_key": routing_key, "body": body})

        source = from_iter(["hello"])
        handle = to_rabbitmq(source, MockChannel(), "my-exchange")

        handle.dispose()

        assert len(published) == 1
        assert published[0]["exchange"] == "my-exchange"
        assert published[0]["routing_key"] == ""
        assert json.loads(published[0]["body"]) == "hello"

    def test_custom_routing_key(self) -> None:
        published: list[Any] = []

        class MockChannel:
            def basic_publish(self, *, exchange: str, routing_key: str, body: bytes) -> None:
                published.append({"exchange": exchange, "routing_key": routing_key, "body": body})

        source = from_iter([{"type": "click", "data": "xyz"}])
        handle = to_rabbitmq(
            source,
            MockChannel(),
            "events",
            routing_key_extractor=lambda v: v["type"],
        )

        handle.dispose()

        assert published[0]["routing_key"] == "click"
