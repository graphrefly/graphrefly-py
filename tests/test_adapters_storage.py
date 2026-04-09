"""Tests for 5.2d storage & sink adapters (src/graphrefly/extra/adapters.py)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import state
from graphrefly.extra.adapters import (
    BufferedSinkHandle,
    SinkHandle,
    SinkTransportError,
    checkpoint_to_redis,
    checkpoint_to_s3,
    from_django_orm,
    from_sqlalchemy,
    from_sqlite,
    from_tortoise,
    to_clickhouse,
    to_csv,
    to_django_orm,
    to_file,
    to_loki,
    to_mongo,
    to_postgres,
    to_s3,
    to_sqlalchemy,
    to_sqlite,
    to_tempo,
    to_tortoise,
)
from graphrefly.extra.sources import from_iter


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
        handle = to_file(source, writer, serialize=bad_serialize, on_transport_error=errors.append)
        assert len(errors) == 1
        assert errors[0].stage == "serialize"
        handle.dispose()

    def test_write_error(self) -> None:
        errors: list[SinkTransportError] = []

        class BadWriter:
            def write(self, _data: Any) -> None:
                raise OSError("disk full")

            def close(self) -> None:
                pass

        source = from_iter([1])
        handle = to_file(source, BadWriter(), on_transport_error=errors.append)
        assert len(errors) == 1
        assert errors[0].stage == "send"
        handle.dispose()

    def test_errors_node(self) -> None:
        """Errors companion node receives transport errors reactively."""

        class BadWriter:
            def write(self, _data: Any) -> None:
                raise OSError("write fail")

            def close(self) -> None:
                pass

        source = from_iter([1])
        handle = to_file(source, BadWriter())
        assert handle.errors.get() is not None
        assert handle.errors.get().stage == "send"  # type: ignore[union-attr]
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
            source,
            MockCH(),
            "t",
            batch_size=10,
            transform=bad_transform,
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
            def put_object(  # noqa: N803
                self,
                *,
                Bucket: str,
                Key: str,
                Body: str,
                ContentType: str = "",
            ) -> None:
                uploads.append({"Bucket": Bucket, "Key": Key, "Body": Body})

        source = from_iter([{"a": 1}, {"b": 2}])
        handle = to_s3(
            source,
            MockS3(),
            "my-bucket",
            batch_size=10,
            key_generator=lambda seq, _ts: f"batch-{seq}.ndjson",
        )
        handle.dispose()
        assert len(uploads) == 1
        assert uploads[0]["Key"] == "batch-1.ndjson"
        assert uploads[0]["Body"] == '{"a": 1}\n{"b": 2}\n'

    def test_json_format(self) -> None:
        uploads: list[dict[str, Any]] = []

        class MockS3:
            def put_object(  # noqa: N803
                self,
                *,
                Bucket: str,
                Key: str,
                Body: str,
                ContentType: str = "",
            ) -> None:
                uploads.append({"Body": Body})

        source = from_iter([1, 2])
        handle = to_s3(
            source,
            MockS3(),
            "b",
            fmt="json",
            batch_size=10,
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
        handle = to_postgres(source, MockPG(), "events")
        assert isinstance(handle, SinkHandle)
        assert len(queries) == 2
        assert 'INSERT INTO "events"' in queries[0]["sql"]
        handle.dispose()

    def test_to_sql_error(self) -> None:
        errors: list[SinkTransportError] = []

        class MockPG:
            def execute(self, sql: str, params: list[Any]) -> None:
                pass

        def bad_sql(_v: Any, _t: str) -> tuple[str, list[Any]]:
            raise ValueError("bad sql")

        source = from_iter([1])
        handle = to_postgres(
            source, MockPG(), "t", to_sql=bad_sql, on_transport_error=errors.append
        )
        assert len(errors) == 1
        assert errors[0].stage == "serialize"
        handle.dispose()

    def test_errors_node_without_callback(self) -> None:
        """Errors node receives errors even without on_transport_error callback."""

        class MockPG:
            def execute(self, sql: str, params: list[Any]) -> None:
                raise RuntimeError("connection lost")

        source = from_iter([1])
        handle = to_postgres(source, MockPG(), "t")
        assert handle.errors.get() is not None
        assert handle.errors.get().stage == "send"  # type: ignore[union-attr]
        handle.dispose()


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
        handle = to_mongo(source, MockCollection())
        assert docs == [{"a": 1}, {"b": 2}]
        handle.dispose()

    def test_custom_to_document(self) -> None:
        docs: list[Any] = []

        class MockCollection:
            def insert_one(self, doc: Any) -> None:
                docs.append(doc)

        source = from_iter([1, 2])
        handle = to_mongo(source, MockCollection(), to_document=lambda v: {"value": v, "ts": "now"})
        assert docs == [{"value": 1, "ts": "now"}, {"value": 2, "ts": "now"}]
        handle.dispose()


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
        handle = to_loki(source, MockLoki(), labels={"job": "test"}, to_line=lambda v: v)
        assert len(pushes) == 1
        assert pushes[0]["streams"][0]["stream"] == {"job": "test"}
        assert pushes[0]["streams"][0]["values"][0][1] == "log line 1"
        handle.dispose()

    def test_dynamic_labels(self) -> None:
        pushes: list[Any] = []

        class MockLoki:
            def push(self, payload: Any) -> None:
                pushes.append(payload)

        source = from_iter([{"level": "error", "msg": "fail"}])
        handle = to_loki(
            source,
            MockLoki(),
            labels={"job": "app"},
            to_line=lambda v: v["msg"],
            to_labels=lambda v: {"level": v["level"]},
        )
        assert pushes[0]["streams"][0]["stream"] == {"job": "app", "level": "error"}
        handle.dispose()


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
        handle = to_tempo(source, MockTempo())
        assert len(pushes) == 1
        assert pushes[0]["resourceSpans"] == [span]
        handle.dispose()


# ——————————————————————————————————————————————————————————————
#  checkpoint_to_s3
# ——————————————————————————————————————————————————————————————


class TestCheckpointToS3:
    def test_adapter_saves_to_s3(self) -> None:
        saved: list[dict[str, Any]] = []

        class MockS3:
            def put_object(  # noqa: N803
                self,
                *,
                Bucket: str,
                Key: str,
                Body: str,
                ContentType: str = "",
            ) -> None:
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
        saved_adapter[0].save("test-graph", {"snapshot": True})
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
        saved_adapter[0].save("my-graph", {"snapshot": True})
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
        self._fail_on_call: int | None = None

    def query(self, sql: str, params: Any = ()) -> list[Any]:
        self.calls.append({"sql": sql, "params": params})
        if self._fail_on_call is not None and len(self.calls) == self._fail_on_call:
            raise RuntimeError("db error")
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
        handle = to_sqlite(source, db, "events")
        assert isinstance(handle, SinkHandle)
        assert len(db.calls) == 2
        assert 'INSERT INTO "events"' in db.calls[0]["sql"]
        assert db.calls[0]["params"] == ['{"x":1}']
        assert db.calls[1]["params"] == ['{"x":2}']
        handle.dispose()

    def test_custom_to_sql(self) -> None:
        db = MockSqliteDb()
        source = from_iter([42])

        def custom_sql(v: Any, t: str) -> tuple[str, list[Any]]:
            return (f'INSERT INTO "{t}" (value) VALUES (?)', [v])

        handle = to_sqlite(source, db, "numbers", to_sql=custom_sql)
        assert db.calls[0]["sql"] == 'INSERT INTO "numbers" (value) VALUES (?)'
        assert db.calls[0]["params"] == [42]
        handle.dispose()

    def test_serialize_error(self) -> None:
        errors: list[SinkTransportError] = []
        db = MockSqliteDb()

        def bad_sql(_v: Any, _t: str) -> tuple[str, list[Any]]:
            raise ValueError("bad sql")

        source = from_iter([1])
        handle = to_sqlite(source, db, "t", to_sql=bad_sql, on_transport_error=errors.append)
        assert len(errors) == 1
        assert errors[0].stage == "serialize"
        handle.dispose()

    def test_send_error(self) -> None:
        errors: list[SinkTransportError] = []
        db = MockSqliteDb(error=RuntimeError("disk full"))
        source = from_iter([{"a": 1}])
        handle = to_sqlite(source, db, "t", on_transport_error=errors.append)
        assert len(errors) == 1
        assert errors[0].stage == "send"
        assert str(errors[0].error) == "disk full"
        handle.dispose()

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

    def test_batch_insert_wraps_in_transaction(self) -> None:
        db = MockSqliteDb()
        source = from_iter([1, 2, 3])
        handle = to_sqlite(source, db, "t", batch_insert=True)
        # BEGIN, INSERT x3, COMMIT
        sqls = [c["sql"] for c in db.calls]
        assert sqls[0] == "BEGIN"
        assert all('INSERT INTO "t"' in s for s in sqls[1:4])
        assert sqls[4] == "COMMIT"
        handle.dispose()

    def test_batch_insert_rollback_on_error(self) -> None:
        errors: list[SinkTransportError] = []
        db = MockSqliteDb()
        # Fail on the 3rd call (second INSERT)
        db._fail_on_call = 3
        source = from_iter([1, 2, 3])
        handle = to_sqlite(source, db, "t", batch_insert=True, on_transport_error=errors.append)
        sqls = [c["sql"] for c in db.calls]
        assert sqls[0] == "BEGIN"
        assert sqls[-1] == "ROLLBACK"
        assert len(errors) == 1
        assert errors[0].stage == "send"
        handle.dispose()

    def test_errors_node_without_callback(self) -> None:
        """Errors node captures errors even without explicit callback."""
        db = MockSqliteDb(error=RuntimeError("fail"))
        source = from_iter([1])
        handle = to_sqlite(source, db, "t")
        assert handle.errors.get() is not None
        assert handle.errors.get().stage == "send"  # type: ignore[union-attr]
        handle.dispose()

    def test_batch_insert_returns_buffered_sink_handle(self) -> None:
        db = MockSqliteDb()
        source = from_iter([1])
        handle = to_sqlite(source, db, "t", batch_insert=True)
        assert isinstance(handle, BufferedSinkHandle)
        assert callable(handle.flush)
        handle.dispose()

    def test_batch_insert_auto_flushes_at_max_batch_size(self) -> None:
        db = MockSqliteDb()
        source = from_iter([1, 2, 3, 4, 5])
        handle = to_sqlite(source, db, "t", batch_insert=True, max_batch_size=2)
        sqls = [c["sql"] for c in db.calls]
        begins = [s for s in sqls if s == "BEGIN"]
        commits = [s for s in sqls if s == "COMMIT"]
        inserts = [s for s in sqls if "INSERT" in s]
        assert len(begins) == 3
        assert len(commits) == 3
        assert len(inserts) == 5
        handle.dispose()

    def test_batch_insert_flushes_on_dispose(self) -> None:
        db = MockSqliteDb()
        s = state(0)
        handle = to_sqlite(s, db, "t", batch_insert=True)
        s.down([(MessageType.DATA, 1)])
        s.down([(MessageType.DATA, 2)])
        # No terminal → no flush yet
        assert not any(c["sql"] == "BEGIN" for c in db.calls)
        handle.dispose()
        sqls = [c["sql"] for c in db.calls]
        assert "BEGIN" in sqls
        assert "COMMIT" in sqls
        assert len([s for s in sqls if "INSERT" in s]) == 2

    def test_batch_insert_dispose_is_idempotent(self) -> None:
        db = MockSqliteDb()
        source = from_iter([1])
        handle = to_sqlite(source, db, "t", batch_insert=True)
        count_before = len(db.calls)
        handle.dispose()
        handle.dispose()  # second call is no-op
        assert len(db.calls) == count_before

    def test_batch_insert_begin_failure_preserves_data(self) -> None:
        errors: list[SinkTransportError] = []
        begin_fails = [True]
        db_calls: list[dict[str, Any]] = []

        class RetryableDb:
            def query(self, sql: str, params: Any = ()) -> list[Any]:
                db_calls.append({"sql": sql, "params": params})
                if sql == "BEGIN" and begin_fails[0]:
                    raise RuntimeError("locked")
                return []

        source = from_iter([1])
        handle = to_sqlite(
            source, RetryableDb(), "t", batch_insert=True, on_transport_error=errors.append
        )
        assert len(errors) == 1
        assert errors[0].error.args[0] == "locked"
        # No INSERTs since BEGIN failed
        assert not any("INSERT" in c["sql"] for c in db_calls)
        # Manual flush after fixing succeeds — data preserved
        begin_fails[0] = False
        handle.flush()
        assert any("INSERT" in c["sql"] for c in db_calls)
        assert db_calls[-1]["sql"] == "COMMIT"
        handle.dispose()

    def test_batch_insert_no_data_no_transaction(self) -> None:
        db = MockSqliteDb()
        source = from_iter([])
        handle = to_sqlite(source, db, "t", batch_insert=True)
        assert not any(c["sql"] == "BEGIN" for c in db.calls)
        handle.dispose()


# ——————————————————————————————————————————————————————————————
#  from_sqlalchemy / to_sqlalchemy
# ——————————————————————————————————————————————————————————————


class MockSqlAlchemySession:
    """Mock SQLAlchemy session with ``execute()``, ``add()``, ``flush()``."""

    def __init__(
        self,
        rows: list[Any] | None = None,
        error: Exception | None = None,
        flush_error: Exception | None = None,
    ) -> None:
        self.rows = rows or []
        self.error = error
        self.flush_error = flush_error
        self.execute_calls: list[dict[str, Any]] = []
        self.added: list[Any] = []
        self.flush_count = 0

    def execute(self, stmt: Any, params: Any = None) -> list[Any]:
        self.execute_calls.append({"stmt": stmt, "params": params})
        if self.error is not None:
            raise self.error
        return list(self.rows)

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    def flush(self) -> None:
        self.flush_count += 1
        if self.flush_error is not None:
            raise self.flush_error


class TestFromSqlAlchemy:
    def test_emits_data_per_row_then_complete(self) -> None:
        session = MockSqlAlchemySession(rows=[{"id": 1}, {"id": 2}])
        source = from_sqlalchemy(session, "SELECT * FROM t")
        from graphrefly.extra.sources import to_list

        result = to_list(source)
        assert result == [{"id": 1}, {"id": 2}]

    def test_passes_params(self) -> None:
        session = MockSqlAlchemySession(rows=[{"id": 1}])
        source = from_sqlalchemy(session, "SELECT * FROM t WHERE id = :id", params={"id": 1})
        from graphrefly.extra.sources import to_list

        to_list(source)
        assert session.execute_calls[0]["params"] == {"id": 1}

    def test_applies_map_row(self) -> None:
        session = MockSqlAlchemySession(rows=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])
        source = from_sqlalchemy(session, "SELECT *", map_row=lambda r: r["name"])
        from graphrefly.extra.sources import to_list

        assert to_list(source) == ["A", "B"]

    def test_error_on_throw(self) -> None:
        session = MockSqlAlchemySession(error=RuntimeError("connection lost"))
        source = from_sqlalchemy(session, "SELECT 1")
        errors: list[Any] = []

        def sink(msgs: Any) -> None:
            for m in msgs:
                if m[0] is MessageType.ERROR:
                    errors.append(m[1])

        source.subscribe(sink)
        assert len(errors) == 1
        assert str(errors[0]) == "connection lost"

    def test_complete_with_zero_rows(self) -> None:
        session = MockSqlAlchemySession(rows=[])
        source = from_sqlalchemy(session, "SELECT * FROM empty")
        from graphrefly.extra.sources import to_list

        assert to_list(source) == []


class TestToSqlAlchemy:
    def test_adds_each_value(self) -> None:
        session = MockSqlAlchemySession()
        source = from_iter(["model_a", "model_b"])
        handle = to_sqlalchemy(source, session)
        assert isinstance(handle, SinkHandle)
        assert session.added == ["model_a", "model_b"]
        assert session.flush_count == 2
        handle.dispose()

    def test_custom_to_model(self) -> None:
        session = MockSqlAlchemySession()
        source = from_iter([{"name": "Alice"}])
        handle = to_sqlalchemy(source, session, to_model=lambda v: f"User({v['name']})")
        assert session.added == ["User(Alice)"]
        handle.dispose()

    def test_auto_flush_false(self) -> None:
        session = MockSqlAlchemySession()
        source = from_iter([1, 2])
        handle = to_sqlalchemy(source, session, auto_flush=False)
        assert session.added == [1, 2]
        assert session.flush_count == 0
        handle.dispose()

    def test_serialize_error(self) -> None:
        errors: list[SinkTransportError] = []
        session = MockSqlAlchemySession()

        def bad_model(_v: Any) -> Any:
            raise ValueError("bad model")

        source = from_iter([1])
        handle = to_sqlalchemy(
            source, session, to_model=bad_model, on_transport_error=errors.append
        )
        assert len(errors) == 1
        assert errors[0].stage == "serialize"
        handle.dispose()

    def test_flush_error(self) -> None:
        errors: list[SinkTransportError] = []
        session = MockSqlAlchemySession(flush_error=RuntimeError("flush failed"))
        source = from_iter([1])
        handle = to_sqlalchemy(source, session, on_transport_error=errors.append)
        assert len(errors) == 1
        assert errors[0].stage == "send"
        assert str(errors[0].error) == "flush failed"
        handle.dispose()

    def test_errors_node_without_callback(self) -> None:
        session = MockSqlAlchemySession(flush_error=RuntimeError("fail"))
        source = from_iter([1])
        handle = to_sqlalchemy(source, session)
        assert handle.errors.get() is not None
        assert handle.errors.get().stage == "send"  # type: ignore[union-attr]
        handle.dispose()

    def test_batch_insert_flushes_on_terminal(self) -> None:
        session = MockSqlAlchemySession()
        source = from_iter([1, 2, 3])
        handle = to_sqlalchemy(source, session, batch_insert=True)
        assert isinstance(handle, BufferedSinkHandle)
        assert session.added == [1, 2, 3]
        assert session.flush_count == 1  # single flush at terminal
        handle.dispose()

    def test_batch_insert_auto_flushes_at_max_size(self) -> None:
        session = MockSqlAlchemySession()
        source = from_iter([1, 2, 3, 4, 5])
        handle = to_sqlalchemy(source, session, batch_insert=True, max_batch_size=2)
        # 2 auto-flushes (at 2, at 4) + 1 terminal flush (at 5)
        assert session.flush_count == 3
        assert session.added == [1, 2, 3, 4, 5]
        handle.dispose()

    def test_batch_insert_dispose_flushes(self) -> None:
        session = MockSqlAlchemySession()
        s = state(0)
        handle = to_sqlalchemy(s, session, batch_insert=True)
        s.down([(MessageType.DATA, "a")])
        s.down([(MessageType.DATA, "b")])
        assert session.flush_count == 0  # no terminal yet
        handle.dispose()
        assert session.flush_count == 1
        assert session.added == ["a", "b"]

    def test_batch_insert_dispose_is_idempotent(self) -> None:
        session = MockSqlAlchemySession()
        source = from_iter([1])
        handle = to_sqlalchemy(source, session, batch_insert=True)
        flush_before = session.flush_count
        handle.dispose()
        handle.dispose()  # second call is no-op
        assert session.flush_count == flush_before

    def test_batch_insert_flush_error(self) -> None:
        errors: list[SinkTransportError] = []
        session = MockSqlAlchemySession(flush_error=RuntimeError("fail"))
        source = from_iter([1])
        handle = to_sqlalchemy(source, session, batch_insert=True, on_transport_error=errors.append)
        assert len(errors) == 1
        assert errors[0].stage == "send"
        handle.dispose()


# ——————————————————————————————————————————————————————————————
#  from_django_orm / to_django_orm
# ——————————————————————————————————————————————————————————————


class MockDjangoManager:
    """Mock Django objects manager with ``create()``."""

    def __init__(self, error: Exception | None = None) -> None:
        self.created: list[dict[str, Any]] = []
        self.error = error

    def create(self, **kwargs: Any) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        self.created.append(kwargs)
        return kwargs


class MockDjangoModel:
    """Mock Django model class with ``objects`` manager."""

    def __init__(self, error: Exception | None = None) -> None:
        self.objects = MockDjangoManager(error=error)


class TestFromDjangoOrm:
    def test_emits_data_per_row_then_complete(self) -> None:
        queryset = [{"id": 1}, {"id": 2}, {"id": 3}]
        source = from_django_orm(queryset)
        from graphrefly.extra.sources import to_list

        assert to_list(source) == [{"id": 1}, {"id": 2}, {"id": 3}]

    def test_applies_map_row(self) -> None:
        queryset = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        source = from_django_orm(queryset, map_row=lambda r: r["name"])
        from graphrefly.extra.sources import to_list

        assert to_list(source) == ["A", "B"]

    def test_error_on_throw(self) -> None:
        def bad_qs() -> Any:
            raise RuntimeError("db error")
            yield  # noqa: F401 — make it a generator

        # Use a class that raises on iteration
        class FailingQs:
            def __iter__(self) -> Any:
                raise RuntimeError("db error")

        source = from_django_orm(FailingQs())
        errors: list[Any] = []

        def sink(msgs: Any) -> None:
            for m in msgs:
                if m[0] is MessageType.ERROR:
                    errors.append(m[1])

        source.subscribe(sink)
        assert len(errors) == 1
        assert str(errors[0]) == "db error"

    def test_complete_with_empty_queryset(self) -> None:
        source = from_django_orm([])
        from graphrefly.extra.sources import to_list

        assert to_list(source) == []


class TestToDjangoOrm:
    def test_creates_each_value_as_dict(self) -> None:
        model = MockDjangoModel()
        source = from_iter([{"name": "Alice"}, {"name": "Bob"}])
        handle = to_django_orm(source, model)
        assert isinstance(handle, SinkHandle)
        assert model.objects.created == [{"name": "Alice"}, {"name": "Bob"}]
        handle.dispose()

    def test_non_dict_wraps_as_data(self) -> None:
        model = MockDjangoModel()
        source = from_iter([42, "hello"])
        handle = to_django_orm(source, model)
        assert model.objects.created == [{"data": 42}, {"data": "hello"}]
        handle.dispose()

    def test_custom_to_kwargs(self) -> None:
        model = MockDjangoModel()
        source = from_iter(["Alice", "Bob"])
        handle = to_django_orm(source, model, to_kwargs=lambda v: {"name": v})
        assert model.objects.created == [{"name": "Alice"}, {"name": "Bob"}]
        handle.dispose()

    def test_serialize_error(self) -> None:
        errors: list[SinkTransportError] = []
        model = MockDjangoModel()
        source = from_iter([1])

        def bad_kwargs(_v: Any) -> dict[str, Any]:
            raise ValueError("bad")

        handle = to_django_orm(
            source, model, to_kwargs=bad_kwargs, on_transport_error=errors.append
        )
        assert len(errors) == 1
        assert errors[0].stage == "serialize"
        handle.dispose()

    def test_send_error(self) -> None:
        errors: list[SinkTransportError] = []
        model = MockDjangoModel(error=RuntimeError("integrity error"))
        source = from_iter([{"name": "Alice"}])
        handle = to_django_orm(source, model, on_transport_error=errors.append)
        assert len(errors) == 1
        assert errors[0].stage == "send"
        assert str(errors[0].error) == "integrity error"
        handle.dispose()

    def test_errors_node_without_callback(self) -> None:
        model = MockDjangoModel(error=RuntimeError("fail"))
        source = from_iter([{"x": 1}])
        handle = to_django_orm(source, model)
        assert handle.errors.get() is not None
        assert handle.errors.get().stage == "send"  # type: ignore[union-attr]
        handle.dispose()


# ——————————————————————————————————————————————————————————————
#  from_tortoise / to_tortoise
# ——————————————————————————————————————————————————————————————


class MockTortoiseModel:
    """Mock Tortoise ORM model class."""

    def __init__(self, save_error: Exception | None = None) -> None:
        self._save_error = save_error
        self.instances: list[MockTortoiseModel] = []
        self._kwargs: dict[str, Any] = {}
        self._parent: MockTortoiseModel | None = None

    def __call__(self, **kwargs: Any) -> MockTortoiseModel:
        inst = MockTortoiseModel(save_error=self._save_error)
        inst._kwargs = kwargs
        inst._parent = self
        return inst

    async def save(self) -> None:
        if self._save_error is not None:
            raise self._save_error
        if self._parent is not None:
            self._parent.instances.append(self)


class TestFromTortoise:
    def test_emits_data_per_row_then_complete(self) -> None:
        # from_tortoise accepts already-awaited lists
        rows = [{"id": 1}, {"id": 2}]
        source = from_tortoise(rows)
        from graphrefly.extra.sources import to_list

        assert to_list(source) == [{"id": 1}, {"id": 2}]

    def test_applies_map_row(self) -> None:
        rows = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        source = from_tortoise(rows, map_row=lambda r: r["name"])
        from graphrefly.extra.sources import to_list

        assert to_list(source) == ["A", "B"]

    def test_error_on_throw(self) -> None:
        class FailingIterable:
            def __iter__(self) -> Any:
                raise RuntimeError("tortoise error")

        source = from_tortoise(FailingIterable())
        errors: list[Any] = []

        def sink(msgs: Any) -> None:
            for m in msgs:
                if m[0] is MessageType.ERROR:
                    errors.append(m[1])

        source.subscribe(sink)
        assert len(errors) == 1
        assert str(errors[0]) == "tortoise error"

    def test_complete_with_empty(self) -> None:
        source = from_tortoise([])
        from graphrefly.extra.sources import to_list

        assert to_list(source) == []

    def test_rejects_coroutine(self) -> None:
        async def coro() -> list[Any]:
            return []

        c = coro()
        try:
            with pytest.raises(TypeError, match="unawaited coroutine"):
                from_tortoise(c)
        finally:
            c.close()

    def test_rejects_async_iterable(self) -> None:
        class AsyncQs:
            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                raise StopAsyncIteration

        with pytest.raises(TypeError, match="async iterable"):
            from_tortoise(AsyncQs())


class TestToTortoise:
    def test_creates_instances_via_save(self) -> None:
        model = MockTortoiseModel()
        source = from_iter([{"name": "Alice"}, {"name": "Bob"}])
        # to_tortoise bridges async save() via run_until_complete internally
        handle = to_tortoise(source, model)
        assert isinstance(handle, SinkHandle)
        assert len(model.instances) == 2
        assert model.instances[0]._kwargs == {"name": "Alice"}
        assert model.instances[1]._kwargs == {"name": "Bob"}
        handle.dispose()

    def test_non_dict_wraps_as_data(self) -> None:
        model = MockTortoiseModel()
        handle = to_tortoise(from_iter([42]), model)
        assert len(model.instances) == 1
        assert model.instances[0]._kwargs == {"data": 42}
        handle.dispose()

    def test_custom_to_kwargs(self) -> None:
        model = MockTortoiseModel()
        handle = to_tortoise(from_iter(["Alice"]), model, to_kwargs=lambda v: {"name": v})
        assert model.instances[0]._kwargs == {"name": "Alice"}
        handle.dispose()

    def test_save_error(self) -> None:
        errors: list[SinkTransportError] = []
        model = MockTortoiseModel(save_error=RuntimeError("db error"))
        handle = to_tortoise(from_iter([{"x": 1}]), model, on_transport_error=errors.append)
        assert len(errors) == 1
        assert errors[0].stage == "send"
        handle.dispose()

    def test_serialize_error(self) -> None:
        errors: list[SinkTransportError] = []
        model = MockTortoiseModel()

        def bad_kwargs(_v: Any) -> dict[str, Any]:
            raise ValueError("bad")

        handle = to_tortoise(
            from_iter([1]), model, to_kwargs=bad_kwargs, on_transport_error=errors.append
        )
        assert len(errors) == 1
        assert errors[0].stage == "serialize"
        handle.dispose()


# ——————————————————————————————————————————————————————————————
#  CancellationToken
# ——————————————————————————————————————————————————————————————


class TestCancellationToken:
    def test_basic_cancel(self) -> None:
        from graphrefly.core.cancellation import cancellation_token

        token = cancellation_token()
        assert not token.is_cancelled
        token.cancel()
        assert token.is_cancelled

    def test_on_cancel_fires(self) -> None:
        from graphrefly.core.cancellation import cancellation_token

        token = cancellation_token()
        fired: list[bool] = []
        token.on_cancel(lambda: fired.append(True))
        token.cancel()
        assert fired == [True]

    def test_on_cancel_after_already_cancelled(self) -> None:
        from graphrefly.core.cancellation import cancellation_token

        token = cancellation_token()
        token.cancel()
        fired: list[bool] = []
        token.on_cancel(lambda: fired.append(True))
        assert fired == [True]

    def test_unsubscribe_on_cancel(self) -> None:
        from graphrefly.core.cancellation import cancellation_token

        token = cancellation_token()
        fired: list[bool] = []
        unsub = token.on_cancel(lambda: fired.append(True))
        unsub()
        token.cancel()
        assert fired == []

    def test_cancel_is_idempotent(self) -> None:
        from graphrefly.core.cancellation import cancellation_token

        token = cancellation_token()
        count: list[int] = [0]
        token.on_cancel(lambda: count.__setitem__(0, count[0] + 1))
        token.cancel()
        token.cancel()
        assert count[0] == 1

    def test_protocol_check(self) -> None:
        from graphrefly.core.cancellation import CancellationToken, cancellation_token

        token = cancellation_token()
        assert isinstance(token, CancellationToken)
