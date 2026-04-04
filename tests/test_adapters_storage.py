"""Tests for 5.2d storage & sink adapters (src/graphrefly/extra/adapters.py)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from graphrefly.core.clock import monotonic_ns
from graphrefly.extra.adapters import (
    BufferedSinkHandle,
    SinkTransportError,
    checkpoint_to_redis,
    checkpoint_to_s3,
    from_sqlite,
    to_clickhouse,
    to_csv,
    to_file,
    to_loki,
    to_mongo,
    to_postgres,
    to_s3,
    to_sqlite,
    to_tempo,
)
from graphrefly.extra.sources import from_iter

NS_PER_SEC = 1_000_000_000


def _wait_for(predicate: Any, timeout_ns: int = 5 * NS_PER_SEC) -> None:
    deadline = monotonic_ns() + timeout_ns
    while not predicate() and monotonic_ns() < deadline:
        time.sleep(0.01)


class MockWriter:
    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.closed = False

    def write(self, data: str | bytes) -> None:
        self.chunks.append(data if isinstance(data, str) else data.decode())

    def close(self) -> None:
        self.closed = True


# ——————————————————————————————————————————————————————————————
#  to_file
# ——————————————————————————————————————————————————————————————


class TestToFile:
    def test_write_through(self) -> None:
        writer = MockWriter()
        source = from_iter([1, 2, 3])
        handle = to_file(source, writer)
        assert isinstance(handle, BufferedSinkHandle)
        assert writer.chunks == ["1\n", "2\n", "3\n"]
        handle.dispose()
        assert writer.closed

    def test_custom_serialize(self) -> None:
        writer = MockWriter()
        source = from_iter(["a", "b"])
        handle = to_file(source, writer, serialize=lambda v: f"LINE:{v}\n")
        assert writer.chunks == ["LINE:a\n", "LINE:b\n"]
        handle.dispose()

    def test_batch_size_buffer(self) -> None:
        writer = MockWriter()
        source = from_iter([1, 2, 3, 4, 5])
        handle = to_file(source, writer, batch_size=2)
        # 2 full batches auto-flushed (1,2) and (3,4), remainder (5) flushed on COMPLETE
        assert writer.chunks == ["1\n2\n", "3\n4\n", "5\n"]
        handle.dispose()

    def test_serialize_error(self) -> None:
        errors: list[SinkTransportError] = []
        writer = MockWriter()

        def bad_serialize(_v: Any) -> str:
            raise ValueError("bad")

        source = from_iter([1])
        handle = to_file(
            source, writer, serialize=bad_serialize, on_transport_error=errors.append
        )
        assert len(errors) == 1
        assert errors[0].stage == "serialize"
        handle.dispose()

    def test_write_error(self) -> None:
        errors: list[SinkTransportError] = []

        class BadWriter:
            def write(self, _data: Any) -> None:
                raise IOError("disk full")

            def close(self) -> None:
                pass

        source = from_iter([1])
        handle = to_file(source, BadWriter(), on_transport_error=errors.append)
        assert len(errors) == 1
        assert errors[0].stage == "send"
        handle.dispose()


# ——————————————————————————————————————————————————————————————
#  to_csv
# ——————————————————————————————————————————————————————————————


class TestToCSV:
    def test_header_and_rows(self) -> None:
        writer = MockWriter()
        source = from_iter([{"name": "Alice", "age": "30"}, {"name": "Bob", "age": "25"}])
        handle = to_csv(source, writer, columns=["name", "age"])
        assert writer.chunks == ["name,age\nAlice,30\n", "Bob,25\n"]
        handle.dispose()

    def test_escape_fields(self) -> None:
        writer = MockWriter()
        source = from_iter([{"val": "has,comma"}, {"val": 'has"quote'}])
        handle = to_csv(source, writer, columns=["val"], write_header=False)
        assert writer.chunks == ['"has,comma"\n', '"has""quote"\n']
        handle.dispose()

    def test_custom_delimiter(self) -> None:
        writer = MockWriter()
        source = from_iter([{"a": "1", "b": "2"}])
        handle = to_csv(source, writer, columns=["a", "b"], delimiter="\t")
        assert writer.chunks == ["a\tb\n1\t2\n"]
        handle.dispose()


# ——————————————————————————————————————————————————————————————
#  to_clickhouse
# ——————————————————————————————————————————————————————————————


class TestToClickHouse:
    def test_batch_size_flush(self) -> None:
        inserted: list[list[Any]] = []

        class MockCH:
            def insert(self, table: str, values: list[Any], *, fmt: str = "") -> None:
                inserted.append(list(values))

        source = from_iter([1, 2, 3])
        handle = to_clickhouse(source, MockCH(), "events", batch_size=2)
        # 1 batch of [1,2] auto-flushed, remaining [3] flushed on COMPLETE
        assert inserted == [[1, 2], [3]]
        handle.dispose()

    def test_custom_transform(self) -> None:
        inserted: list[list[Any]] = []

        class MockCH:
            def insert(self, table: str, values: list[Any], *, fmt: str = "") -> None:
                inserted.append(list(values))

        source = from_iter([1, 2])
        handle = to_clickhouse(
            source, MockCH(), "t", batch_size=10, transform=lambda v: {"val": v * 10}
        )
        handle.dispose()
        assert inserted == [[{"val": 10}, {"val": 20}]]

    def test_transform_error(self) -> None:
        errors: list[SinkTransportError] = []

        class MockCH:
            def insert(self, table: str, values: list[Any], *, fmt: str = "") -> None:
                pass

        source = from_iter([1])

        def bad_transform(_v: Any) -> Any:
            raise ValueError("bad transform")

        handle = to_clickhouse(
            source, MockCH(), "t", batch_size=10, transform=bad_transform,
            on_transport_error=errors.append,
        )
        assert len(errors) == 1
        assert errors[0].stage == "serialize"
        handle.dispose()


# ——————————————————————————————————————————————————————————————
#  to_s3
# ——————————————————————————————————————————————————————————————


class TestToS3:
    def test_ndjson_upload(self) -> None:
        uploads: list[dict[str, Any]] = []

        class MockS3:
            def put_object(self, *, Bucket: str, Key: str, Body: str, ContentType: str = "") -> None:
                uploads.append({"Bucket": Bucket, "Key": Key, "Body": Body})

        source = from_iter([{"a": 1}, {"b": 2}])
        handle = to_s3(
            source, MockS3(), "my-bucket", batch_size=10,
            key_generator=lambda seq, _ts: f"batch-{seq}.ndjson",
        )
        handle.dispose()
        assert len(uploads) == 1
        assert uploads[0]["Key"] == "batch-1.ndjson"
        assert uploads[0]["Body"] == '{"a": 1}\n{"b": 2}\n'

    def test_json_format(self) -> None:
        uploads: list[dict[str, Any]] = []

        class MockS3:
            def put_object(self, *, Bucket: str, Key: str, Body: str, ContentType: str = "") -> None:
                uploads.append({"Body": Body})

        source = from_iter([1, 2])
        handle = to_s3(
            source, MockS3(), "b", fmt="json", batch_size=10,
            key_generator=lambda seq, _ts: f"batch-{seq}.json",
        )
        handle.dispose()
        assert uploads[0]["Body"] == "[1, 2]"


# ——————————————————————————————————————————————————————————————
#  to_postgres
# ——————————————————————————————————————————————————————————————


class TestToPostgres:
    def test_insert_rows(self) -> None:
        queries: list[dict[str, Any]] = []

        class MockPG:
            def execute(self, sql: str, params: list[Any]) -> None:
                queries.append({"sql": sql, "params": params})

        source = from_iter([{"x": 1}, {"x": 2}])
        unsub = to_postgres(source, MockPG(), "events")
        assert len(queries) == 2
        assert 'INSERT INTO "events"' in queries[0]["sql"]
        unsub()

    def test_to_sql_error(self) -> None:
        errors: list[SinkTransportError] = []

        class MockPG:
            def execute(self, sql: str, params: list[Any]) -> None:
                pass

        def bad_sql(_v: Any, _t: str) -> tuple[str, list[Any]]:
            raise ValueError("bad sql")

        source = from_iter([1])
        unsub = to_postgres(
            source, MockPG(), "t", to_sql=bad_sql, on_transport_error=errors.append
        )
        assert len(errors) == 1
        assert errors[0].stage == "serialize"
        unsub()


# ——————————————————————————————————————————————————————————————
#  to_mongo
# ——————————————————————————————————————————————————————————————


class TestToMongo:
    def test_insert_documents(self) -> None:
        docs: list[Any] = []

        class MockCollection:
            def insert_one(self, doc: Any) -> None:
                docs.append(doc)

        source = from_iter([{"a": 1}, {"b": 2}])
        unsub = to_mongo(source, MockCollection())
        assert docs == [{"a": 1}, {"b": 2}]
        unsub()

    def test_custom_to_document(self) -> None:
        docs: list[Any] = []

        class MockCollection:
            def insert_one(self, doc: Any) -> None:
                docs.append(doc)

        source = from_iter([1, 2])
        unsub = to_mongo(
            source, MockCollection(), to_document=lambda v: {"value": v, "ts": "now"}
        )
        assert docs == [{"value": 1, "ts": "now"}, {"value": 2, "ts": "now"}]
        unsub()


# ——————————————————————————————————————————————————————————————
#  to_loki
# ——————————————————————————————————————————————————————————————


class TestToLoki:
    def test_push_log_entries(self) -> None:
        pushes: list[Any] = []

        class MockLoki:
            def push(self, payload: Any) -> None:
                pushes.append(payload)

        source = from_iter(["log line 1"])
        unsub = to_loki(source, MockLoki(), labels={"job": "test"}, to_line=lambda v: v)
        assert len(pushes) == 1
        assert pushes[0]["streams"][0]["stream"] == {"job": "test"}
        assert pushes[0]["streams"][0]["values"][0][1] == "log line 1"
        unsub()

    def test_dynamic_labels(self) -> None:
        pushes: list[Any] = []

        class MockLoki:
            def push(self, payload: Any) -> None:
                pushes.append(payload)

        source = from_iter([{"level": "error", "msg": "fail"}])
        unsub = to_loki(
            source, MockLoki(),
            labels={"job": "app"},
            to_line=lambda v: v["msg"],
            to_labels=lambda v: {"level": v["level"]},
        )
        assert pushes[0]["streams"][0]["stream"] == {"job": "app", "level": "error"}
        unsub()


# ——————————————————————————————————————————————————————————————
#  to_tempo
# ——————————————————————————————————————————————————————————————


class TestToTempo:
    def test_push_spans(self) -> None:
        pushes: list[Any] = []

        class MockTempo:
            def push(self, payload: Any) -> None:
                pushes.append(payload)

        span = {"traceId": "abc", "spans": [{"name": "op1"}]}
        source = from_iter([span])
        unsub = to_tempo(source, MockTempo())
        assert len(pushes) == 1
        assert pushes[0]["resourceSpans"] == [span]
        unsub()


# ——————————————————————————————————————————————————————————————
#  checkpoint_to_s3
# ——————————————————————————————————————————————————————————————


class TestCheckpointToS3:
    def test_adapter_saves_to_s3(self) -> None:
        saved: list[dict[str, Any]] = []

        class MockS3:
            def put_object(self, *, Bucket: str, Key: str, Body: str, ContentType: str = "") -> None:
                saved.append({"Bucket": Bucket, "Key": Key, "Body": Body})

        saved_adapter: list[Any] = []

        class MockGraph:
            name = "test-graph"

            def auto_checkpoint(self, adapter: Any, **kwargs: Any) -> Any:
                saved_adapter.append(adapter)

                class Handle:
                    def dispose(self) -> None:
                        pass

                return Handle()

        handle = checkpoint_to_s3(MockGraph(), MockS3(), "my-bucket", prefix="cp/")
        assert hasattr(handle, "dispose")
        saved_adapter[0].save({"snapshot": True})
        assert len(saved) == 1
        assert saved[0]["Bucket"] == "my-bucket"
        assert saved[0]["Key"].startswith("cp/test-graph/checkpoint-")


# ——————————————————————————————————————————————————————————————
#  checkpoint_to_redis
# ——————————————————————————————————————————————————————————————


class TestCheckpointToRedis:
    def test_adapter_saves_to_redis(self) -> None:
        saved: list[dict[str, Any]] = []

        class MockRedis:
            def set(self, key: str, value: str) -> None:
                saved.append({"key": key, "value": value})

            def get(self, key: str) -> str | None:
                return None

        saved_adapter: list[Any] = []

        class MockGraph:
            name = "my-graph"

            def auto_checkpoint(self, adapter: Any, **kwargs: Any) -> Any:
                saved_adapter.append(adapter)

                class Handle:
                    def dispose(self) -> None:
                        pass

                return Handle()

        handle = checkpoint_to_redis(MockGraph(), MockRedis())
        assert hasattr(handle, "dispose")
        saved_adapter[0].save({"snapshot": True})
        assert len(saved) == 1
        assert saved[0]["key"] == "graphrefly:checkpoint:my-graph"
        assert json.loads(saved[0]["value"]) == {"snapshot": True}


# ——————————————————————————————————————————————————————————————
#  from_sqlite
# ——————————————————————————————————————————————————————————————


class MockSqliteDb:
    """Mock SQLite database with a ``query`` method."""

    def __init__(self, rows: list[Any] | None = None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def query(self, sql: str, params: tuple = ()) -> list[Any]:
        self.calls.append({"sql": sql, "params": params})
        if self.error is not None:
            raise self.error
        return list(self.rows)


class TestFromSqlite:
    def test_emits_data_per_row_then_complete(self) -> None:
        db = MockSqliteDb(rows=[{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}])
        source = from_sqlite(db, "SELECT * FROM users")
        from graphrefly.extra.sources import to_list

        result = to_list(source)
        assert result == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]

    def test_passes_params(self) -> None:
        db = MockSqliteDb(rows=[{"id": 1}])
        source = from_sqlite(db, "SELECT * FROM users WHERE id = ?", params=(1,))
        from graphrefly.extra.sources import to_list

        to_list(source)
        assert db.calls[0]["params"] == (1,)

    def test_applies_map_row(self) -> None:
        db = MockSqliteDb(rows=[{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}])
        source = from_sqlite(db, "SELECT * FROM users", map_row=lambda r: r["name"])
        from graphrefly.extra.sources import to_list

        result = to_list(source)
        assert result == ["Alice", "Bob"]

    def test_error_on_throw(self) -> None:
        db = MockSqliteDb(error=RuntimeError("db locked"))
        source = from_sqlite(db, "SELECT 1")
        from graphrefly.core.protocol import MessageType

        errors: list[Any] = []

        def sink(msgs: Any) -> None:
            for m in msgs:
                if m[0] is MessageType.ERROR:
                    errors.append(m[1])

        source.subscribe(sink)
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert str(errors[0]) == "db locked"

    def test_complete_with_zero_rows(self) -> None:
        db = MockSqliteDb(rows=[])
        source = from_sqlite(db, "SELECT * FROM empty_table")
        from graphrefly.extra.sources import to_list

        result = to_list(source)
        assert result == []

    def test_error_with_no_partial_data_when_map_row_throws(self) -> None:
        call_count = 0

        def bad_map(r: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ValueError("bad row")
            return r

        db = MockSqliteDb(rows=[{"v": 1}, {"v": 2}, {"v": 3}])
        source = from_sqlite(db, "SELECT v FROM t", map_row=bad_map)
        from graphrefly.core.protocol import MessageType

        msgs: list[Any] = []

        def sink(m: Any) -> None:
            for msg in m:
                msgs.append(msg)

        source.subscribe(sink)
        # Pre-map: error before batch, no partial DATA
        assert len(msgs) == 1
        assert msgs[0][0] is MessageType.ERROR
        assert str(msgs[0][1]) == "bad row"


# ——————————————————————————————————————————————————————————————
#  to_sqlite
# ——————————————————————————————————————————————————————————————


class TestToSqlite:
    def test_inserts_each_value(self) -> None:
        db = MockSqliteDb()
        source = from_iter([{"x": 1}, {"x": 2}])
        unsub = to_sqlite(source, db, "events")
        assert len(db.calls) == 2
        assert 'INSERT INTO "events"' in db.calls[0]["sql"]
        assert db.calls[0]["params"] == ['{"x":1}']
        assert db.calls[1]["params"] == ['{"x":2}']
        unsub()

    def test_custom_to_sql(self) -> None:
        db = MockSqliteDb()
        source = from_iter([42])

        def custom_sql(v: Any, t: str) -> tuple[str, list[Any]]:
            return (f'INSERT INTO "{t}" (value) VALUES (?)', [v])

        unsub = to_sqlite(source, db, "numbers", to_sql=custom_sql)
        assert db.calls[0]["sql"] == 'INSERT INTO "numbers" (value) VALUES (?)'
        assert db.calls[0]["params"] == [42]
        unsub()

    def test_serialize_error(self) -> None:
        errors: list[SinkTransportError] = []
        db = MockSqliteDb()

        def bad_sql(_v: Any, _t: str) -> tuple[str, list[Any]]:
            raise ValueError("bad sql")

        source = from_iter([1])
        unsub = to_sqlite(
            source, db, "t", to_sql=bad_sql, on_transport_error=errors.append
        )
        assert len(errors) == 1
        assert errors[0].stage == "serialize"
        unsub()

    def test_send_error(self) -> None:
        errors: list[SinkTransportError] = []
        db = MockSqliteDb(error=RuntimeError("disk full"))
        source = from_iter([{"a": 1}])
        unsub = to_sqlite(source, db, "t", on_transport_error=errors.append)
        assert len(errors) == 1
        assert errors[0].stage == "send"
        assert str(errors[0].error) == "disk full"
        unsub()

    def test_rejects_empty_table_name(self) -> None:
        db = MockSqliteDb()
        source = from_iter([1])
        with pytest.raises(ValueError, match="invalid table name"):
            to_sqlite(source, db, "")

    def test_rejects_null_byte_in_table_name(self) -> None:
        db = MockSqliteDb()
        source = from_iter([1])
        with pytest.raises(ValueError, match="invalid table name"):
            to_sqlite(source, db, "foo\x00bar")
