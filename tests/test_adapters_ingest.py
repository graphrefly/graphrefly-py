"""Tests for 5.3b ingest adapters (src/graphrefly/extra/adapters.py)."""

from __future__ import annotations

import json
import threading
import time
import unittest.mock
from typing import Any

import pytest

from graphrefly.core import MessageType
from graphrefly.core.clock import monotonic_ns
from graphrefly.extra.adapters import (
    from_clickhouse_watch,
    from_csv,
    from_kafka,
    from_ndjson,
    from_otel,
    from_prometheus,
    from_redis_stream,
    from_statsd,
    from_syslog,
    parse_prometheus_text,
    parse_statsd,
    parse_syslog,
    to_kafka,
    to_redis_stream,
)
from graphrefly.extra.sources import from_iter, to_list

NS_PER_SEC = 1_000_000_000


def _wait_for(predicate: Any, timeout_ns: int = 5 * NS_PER_SEC) -> None:
    deadline = monotonic_ns() + timeout_ns
    while not predicate() and monotonic_ns() < deadline:
        time.sleep(0.01)


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
        assert result["facility"] == 4  # 34 >> 3
        assert result["severity"] == 2  # 34 & 7
        assert result["hostname"] == "host"
        assert result["app_name"] == "app"
        assert result["proc_id"] == "1234"
        assert result["msg_id"] == "ID001"
        assert result["message"] == "This is a test"
        assert "timestamp_ns" in result

    def test_unparseable_fallback(self) -> None:
        raw = "some random log line"
        result = parse_syslog(raw)
        assert result["facility"] == 1
        assert result["severity"] == 6
        assert result["message"] == "some random log line"
        assert result["hostname"] == "-"


# ——————————————————————————————————————————————————————————————
#  parse_statsd
# ——————————————————————————————————————————————————————————————


class TestParseStatsD:
    def test_counter(self) -> None:
        result = parse_statsd("page.views:1|c")
        assert result["name"] == "page.views"
        assert result["value"] == 1.0
        assert result["type"] == "counter"

    def test_gauge(self) -> None:
        result = parse_statsd("cpu.usage:72.5|g")
        assert result["name"] == "cpu.usage"
        assert result["value"] == 72.5
        assert result["type"] == "gauge"

    def test_timer(self) -> None:
        result = parse_statsd("request.latency:320|ms")
        assert result["type"] == "timer"
        assert result["value"] == 320.0

    def test_histogram(self) -> None:
        result = parse_statsd("request.size:1024|h")
        assert result["type"] == "histogram"

    def test_sample_rate(self) -> None:
        result = parse_statsd("page.views:1|c|@0.1")
        assert result["sample_rate"] == 0.1

    def test_tags(self) -> None:
        result = parse_statsd("page.views:1|c|#env:prod,region:us")
        assert result["tags"] == {"env": "prod", "region": "us"}

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
        assert m0["name"] == "http_requests_total"
        assert m0["labels"] == {"method": "GET", "code": "200"}
        assert m0["value"] == 1027.0
        assert m0["timestamp_ms"] == 1395066363000.0
        assert m0["type"] == "counter"
        assert m0["help"] == "Total HTTP requests"

        m1 = metrics[1]
        assert m1["name"] == "http_requests_total"
        assert m1["labels"] == {"method": "POST", "code": "200"}
        assert m1["value"] == 42.0
        assert "timestamp_ms" not in m1

        m2 = metrics[2]
        assert m2["name"] == "process_cpu_seconds"
        assert m2["labels"] == {}
        assert m2["value"] == 0.44
        assert m2["type"] == "gauge"

    def test_blank_and_comment_lines(self) -> None:
        text = """\
# This is a comment
# TYPE foo gauge

foo 1
"""
        metrics = parse_prometheus_text(text)
        assert len(metrics) == 1
        assert metrics[0]["name"] == "foo"


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
        assert received[0]["name"] == "up"
        assert received[0]["value"] == 1.0


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
        _wait_for(lambda: len(received) > 0)
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
        unsub = to_kafka(source, producer, "output")

        _wait_for(lambda: len(sent) >= 3)
        unsub()

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

        _wait_for(lambda: len(received) >= 2)
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
        unsub = to_redis_stream(source, client, "mystream")

        _wait_for(lambda: len(added) >= 2)
        unsub()

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

        _wait_for(lambda: len(received) >= 1)
        unsub()

        assert received[0]["facility"] == 4
        assert received[0]["message"] == "test msg"


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

        _wait_for(lambda: len(received) >= 1)
        unsub()

        assert received[0]["name"] == "req.count"
        assert received[0]["tags"] == {"env": "prod"}
