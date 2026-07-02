import asyncio
from collections.abc import AsyncIterable, Awaitable, Callable

import pytest

import graphrefly
from graphrefly import (
    ControlMessage,
    DataMessage,
    ErrorMessage,
    Graph,
    GraphReflyRuntimeError,
    GraphReflyValueError,
    HttpRequest,
    HttpResponse,
    HttpStreamChunkEvent,
    HttpStreamCompleteEvent,
    HttpStreamDriverEvent,
    HttpStreamErrorEvent,
    HttpStreamHead,
    HttpStreamHeadEvent,
    Message,
    SseEvent,
    from_http,
    from_sse,
)


class ManualTask:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class ManualRunner:
    def __init__(self) -> None:
        self.jobs: list[Callable[[], Awaitable[None]]] = []
        self.tasks: list[ManualTask] = []

    def spawn(self, job: Callable[[], Awaitable[None]]) -> ManualTask:
        task = ManualTask()
        self.jobs.append(job)
        self.tasks.append(task)
        return task

    def cancel(self, task: object | None) -> None:
        if isinstance(task, ManualTask):
            task.cancel()

    def run(self, index: int = 0) -> None:
        asyncio.run(self.jobs[index]())


class EagerRunner(ManualRunner):
    def spawn(self, job: Callable[[], Awaitable[None]]) -> ManualTask:
        task = super().spawn(job)
        asyncio.run(job())
        return task


class ManualHttpDriver:
    def __init__(self, response: HttpResponse | BaseException) -> None:
        self.response = response
        self.requests: list[HttpRequest] = []

    async def request(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class ManualHttpStreamDriver:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.requests: list[HttpRequest] = []
        self.started = 0
        self.closed = 0

    def stream(self, request: HttpRequest) -> AsyncIterable[object]:
        self.requests.append(request)
        driver = self

        async def values():
            driver.started += 1
            try:
                for event in driver.events:
                    yield event
            finally:
                driver.closed += 1

        return values()


class DirectHttpStreamDriver:
    def __init__(self, stream: AsyncIterable[object]) -> None:
        self.stream_value = stream
        self.requests: list[HttpRequest] = []

    def stream(self, request: HttpRequest) -> AsyncIterable[object]:
        self.requests.append(request)
        return self.stream_value


class ClosableAsyncStream:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.index = 0
        self.closed = 0

    def __aiter__(self) -> "ClosableAsyncStream":
        return self

    async def __anext__(self) -> object:
        if self.index >= len(self.events):
            raise StopAsyncIteration
        event = self.events[self.index]
        self.index += 1
        return event

    async def aclose(self) -> None:
        self.closed += 1


class OperationCancelled(Exception):
    pass


def error_messages(seen: list[Message[object]]) -> list[str]:
    return [msg.error.message for msg in seen if isinstance(msg, ErrorMessage)]


def data_values(seen: list[Message[object]]) -> list[object]:
    return [msg.value for msg in seen if isinstance(msg, DataMessage)]


def test_network_adapter_public_surface_and_dataclasses():
    for name in [
        "HttpRequest",
        "HttpResponse",
        "HttpStreamHead",
        "HttpStreamHeadEvent",
        "HttpStreamChunkEvent",
        "HttpStreamDriverEvent",
        "HttpStreamErrorEvent",
        "HttpStreamCompleteEvent",
        "SseEvent",
        "LocalHttpDriver",
        "LocalHttpStreamDriver",
        "from_http",
        "from_sse",
    ]:
        assert name in graphrefly.__all__
        assert hasattr(graphrefly, name)
    assert not hasattr(graphrefly, "SseParser")
    assert "RuntimeDriver" not in graphrefly.__all__
    assert HttpStreamDriverEvent.__name__ == "HttpStreamDriverEvent"


def test_from_http_emits_response_data_then_complete():
    graph = Graph("py-from-http-smoke")
    runner = ManualRunner()
    driver = ManualHttpDriver(
        HttpResponse(
            status=201,
            headers=(("content-type", "application/json"),),
            body=b'{"ok":true}',
        )
    )
    seen: list[Message[object]] = []

    node = from_http(
        graph,
        HttpRequest(
            method="POST",
            url="https://example.test/items",
            headers=(("x-test", "1"),),
            body=b"{}",
        ),
        driver=driver,
        runner=runner,
        name="http",
    )
    with node.subscribe(seen.append):
        runner.run()

    assert driver.requests == [
        HttpRequest(
            method="POST",
            url="https://example.test/items",
            headers=(("x-test", "1"),),
            body=b"{}",
        )
    ]
    assert data_values(seen) == [
        HttpResponse(
            status=201,
            headers=(("content-type", "application/json"),),
            body=b'{"ok":true}',
        )
    ]
    assert seen[-1] == ControlMessage("COMPLETE")


def test_from_http_driver_error_becomes_graph_error():
    graph = Graph("py-from-http-error")
    runner = ManualRunner()
    driver = ManualHttpDriver(GraphReflyRuntimeError("network down"))
    seen: list[Message[object]] = []

    node = from_http(graph, "https://example.test", driver=driver, runner=runner)
    with node.subscribe(seen.append):
        runner.run()

    assert error_messages(seen) == ["network down"]
    assert not node.has_value


def test_from_http_requires_explicit_runner_and_driver():
    graph = Graph("py-from-http-missing")
    runner = ManualRunner()

    with pytest.raises(GraphReflyValueError, match="explicit AsyncRunner"):
        from_http(graph, "https://example.test", driver=ManualHttpDriver(HttpResponse(200)))
    with pytest.raises(GraphReflyValueError, match="LocalHttpDriver is required"):
        from_http(graph, "https://example.test", runner=runner)


def test_from_http_deactivation_cancels_job_before_driver_runs():
    graph = Graph("py-from-http-cancel")
    runner = ManualRunner()
    driver = ManualHttpDriver(HttpResponse(status=200, body=b"late"))
    seen: list[Message[object]] = []

    node = from_http(graph, "https://example.test", driver=driver, runner=runner)
    sub = node.subscribe(seen.append)
    sub.unsubscribe()
    runner.run()

    assert runner.tasks[0].cancelled is True
    assert driver.requests == []
    assert not any(isinstance(msg, ErrorMessage) for msg in seen)
    assert not node.has_value


def test_from_sse_http_stream_fallback_parses_split_events_and_complete():
    graph = Graph("py-from-sse-happy")
    runner = ManualRunner()
    driver = ManualHttpStreamDriver(
        [
            HttpStreamHeadEvent(
                HttpStreamHead(
                    status=200,
                    headers=(("content-type", "text/event-stream; charset=utf-8"),),
                )
            ),
            HttpStreamChunkEvent(b": ignored\r\nevent: message\r\nid: 42\r\n"),
            HttpStreamChunkEvent(b"data: caf"),
            HttpStreamChunkEvent(b"\xc3"),
            HttpStreamChunkEvent(b"\xa9\r\nretry: 500\r\nunknown: nope\r\n\r\n"),
            HttpStreamCompleteEvent(),
        ]
    )
    seen: list[Message[object]] = []

    node = from_sse(graph, "https://example.test/events", stream_driver=driver, runner=runner)
    with node.subscribe(seen.append):
        runner.run()

    assert driver.requests == [
        HttpRequest(
            method="GET",
            url="https://example.test/events",
            headers=(("Accept", "text/event-stream"),),
        )
    ]
    assert data_values(seen) == [
        SseEvent(event="message", data="café", id="42", retry_ms=500)
    ]
    assert seen[-1] == ControlMessage("COMPLETE")


def test_from_sse_rejects_bad_head_and_ignores_later_chunks():
    graph = Graph("py-from-sse-bad-head")
    runner = ManualRunner()
    driver = ManualHttpStreamDriver(
        [
            HttpStreamHeadEvent(
                HttpStreamHead(status=404, headers=(("content-type", "text/event-stream"),))
            ),
            HttpStreamChunkEvent(b"data: late\n\n"),
            HttpStreamCompleteEvent(),
        ]
    )
    seen: list[Message[object]] = []

    node = from_sse(graph, "https://example.test/events", stream_driver=driver, runner=runner)
    with node.subscribe(seen.append):
        runner.run()

    assert error_messages(seen) == ["from_sse: unacceptable http status 404"]
    assert data_values(seen) == []
    assert driver.closed == 1


def test_from_sse_closes_host_stream_on_terminal_lifecycle_error():
    graph = Graph("py-from-sse-close-on-error")
    runner = ManualRunner()
    stream = ClosableAsyncStream(
        [
            HttpStreamHeadEvent(
                HttpStreamHead(status=503, headers=(("content-type", "text/event-stream"),))
            ),
            HttpStreamChunkEvent(b"data: late\n\n"),
        ]
    )
    driver = DirectHttpStreamDriver(stream)
    seen: list[Message[object]] = []

    node = from_sse(graph, "https://example.test/events", stream_driver=driver, runner=runner)
    with node.subscribe(seen.append):
        runner.run()

    assert error_messages(seen) == ["from_sse: unacceptable http status 503"]
    assert data_values(seen) == []
    assert stream.closed == 1


def test_from_sse_rejects_illegal_stream_lifecycle():
    graph = Graph("py-from-sse-lifecycle")
    runner = ManualRunner()
    chunk_first = ManualHttpStreamDriver(
        [HttpStreamChunkEvent(b"data: nope\n\n"), HttpStreamCompleteEvent()]
    )
    duplicate_head = ManualHttpStreamDriver(
        [
            HttpStreamHeadEvent(
                HttpStreamHead(status=200, headers=(("content-type", "text/event-stream"),))
            ),
            HttpStreamHeadEvent(
                HttpStreamHead(status=200, headers=(("content-type", "text/event-stream"),))
            ),
            HttpStreamCompleteEvent(),
        ]
    )
    chunk_seen: list[Message[object]] = []
    duplicate_seen: list[Message[object]] = []

    chunk_node = from_sse(
        graph,
        "https://example.test/chunk",
        stream_driver=chunk_first,
        runner=runner,
    )
    duplicate_node = from_sse(
        graph,
        "https://example.test/duplicate",
        stream_driver=duplicate_head,
        runner=runner,
    )
    with chunk_node.subscribe(chunk_seen.append), duplicate_node.subscribe(duplicate_seen.append):
        runner.run(0)
        runner.run(1)

    assert error_messages(chunk_seen) == [
        "from_sse: http stream chunk arrived before response head"
    ]
    assert error_messages(duplicate_seen) == [
        "from_sse: http stream emitted duplicate response head"
    ]


def test_from_sse_complete_flushes_buffered_data_and_ignores_empty_tail():
    graph = Graph("py-from-sse-complete-flush")
    runner = ManualRunner()
    driver = ManualHttpStreamDriver(
        [
            HttpStreamHeadEvent(
                HttpStreamHead(status=204, headers=(("content-type", "text/event-stream"),))
            ),
            HttpStreamChunkEvent(b"event: tail\ndata: final"),
            HttpStreamCompleteEvent(),
        ]
    )
    no_data_driver = ManualHttpStreamDriver(
        [
            HttpStreamHeadEvent(
                HttpStreamHead(status=200, headers=(("content-type", "text/event-stream"),))
            ),
            HttpStreamChunkEvent(b"event: no-data"),
            HttpStreamCompleteEvent(),
        ]
    )
    seen: list[Message[object]] = []
    no_data_seen: list[Message[object]] = []

    node = from_sse(graph, "https://example.test/tail", stream_driver=driver, runner=runner)
    no_data_node = from_sse(
        graph,
        "https://example.test/no-data",
        stream_driver=no_data_driver,
        runner=runner,
    )
    with node.subscribe(seen.append), no_data_node.subscribe(no_data_seen.append):
        runner.run(0)
        runner.run(1)

    assert data_values(seen) == [SseEvent(event="tail", data="final")]
    assert seen[-1] == ControlMessage("COMPLETE")
    assert data_values(no_data_seen) == []
    assert no_data_seen[-1] == ControlMessage("COMPLETE")


def test_from_sse_invalid_utf8_and_parser_overflow_emit_error():
    graph = Graph("py-from-sse-parser-errors")
    runner = ManualRunner()
    invalid_utf8 = ManualHttpStreamDriver(
        [
            HttpStreamHeadEvent(
                HttpStreamHead(status=200, headers=(("content-type", "text/event-stream"),))
            ),
            HttpStreamChunkEvent(b"data: ok\n\n"),
            HttpStreamChunkEvent(b"\xff"),
            HttpStreamCompleteEvent(),
        ]
    )
    overflow = ManualHttpStreamDriver(
        [
            HttpStreamHeadEvent(
                HttpStreamHead(status=200, headers=(("content-type", "text/event-stream"),))
            ),
            HttpStreamChunkEvent(b"x" * (64 * 1024 + 1)),
            HttpStreamCompleteEvent(),
        ]
    )
    invalid_seen: list[Message[object]] = []
    overflow_seen: list[Message[object]] = []

    invalid_node = from_sse(
        graph,
        "https://example.test/invalid",
        stream_driver=invalid_utf8,
        runner=runner,
    )
    overflow_node = from_sse(
        graph,
        "https://example.test/overflow",
        stream_driver=overflow,
        runner=runner,
    )
    with invalid_node.subscribe(invalid_seen.append), overflow_node.subscribe(overflow_seen.append):
        runner.run(0)
        runner.run(1)

    assert data_values(invalid_seen) == [SseEvent(data="ok")]
    assert error_messages(invalid_seen) == ["from_sse: invalid utf-8 in event stream"]
    assert error_messages(overflow_seen) == ["from_sse: parser overflow"]


def test_from_sse_explicit_stream_error_is_not_swallowed_as_cancellation():
    graph = Graph("py-from-sse-explicit-cancel-named-error")
    runner = ManualRunner()
    driver = ManualHttpStreamDriver(
        [
            HttpStreamHeadEvent(
                HttpStreamHead(status=200, headers=(("content-type", "text/event-stream"),))
            ),
            HttpStreamErrorEvent(OperationCancelled("explicit terminal")),
            HttpStreamChunkEvent(b"data: late\n\n"),
        ]
    )
    seen: list[Message[object]] = []

    node = from_sse(graph, "https://example.test/events", stream_driver=driver, runner=runner)
    with node.subscribe(seen.append):
        runner.run()

    assert error_messages(seen) == ["explicit terminal"]
    assert data_values(seen) == []
    assert driver.closed == 1


def test_from_sse_ignores_overlong_retry_without_parser_error():
    graph = Graph("py-from-sse-overlong-retry")
    runner = ManualRunner()
    driver = ManualHttpStreamDriver(
        [
            HttpStreamHeadEvent(
                HttpStreamHead(status=200, headers=(("content-type", "text/event-stream"),))
            ),
            HttpStreamChunkEvent(b"retry: " + (b"9" * 100) + b"\ndata: ok\n\n"),
            HttpStreamCompleteEvent(),
        ]
    )
    seen: list[Message[object]] = []

    node = from_sse(graph, "https://example.test/events", stream_driver=driver, runner=runner)
    with node.subscribe(seen.append):
        runner.run()

    assert data_values(seen) == [SseEvent(data="ok")]
    assert seen[-1] == ControlMessage("COMPLETE")


def test_from_sse_no_hidden_reconnect_retry_or_last_event_id():
    graph = Graph("py-from-sse-no-reconnect")
    runner = ManualRunner()
    driver = ManualHttpStreamDriver(
        [
            HttpStreamHeadEvent(
                HttpStreamHead(status=200, headers=(("content-type", "text/event-stream"),))
            ),
            HttpStreamChunkEvent(b"retry: 25\ndata: first\n\n"),
            HttpStreamErrorEvent(GraphReflyRuntimeError("closed")),
        ]
    )
    seen: list[Message[object]] = []

    node = from_sse(graph, "https://example.test/events", stream_driver=driver, runner=runner)
    with node.subscribe(seen.append):
        runner.run()

    assert driver.started == 1
    assert data_values(seen) == [SseEvent(data="first", retry_ms=25)]
    assert error_messages(seen) == ["closed"]
    assert all(key.lower() != "last-event-id" for key, _ in driver.requests[0].headers)


def test_from_sse_synchronous_runner_completion_is_fenced():
    graph = Graph("py-from-sse-eager-runner")
    runner = EagerRunner()
    driver = ManualHttpStreamDriver(
        [
            HttpStreamHeadEvent(
                HttpStreamHead(status=200, headers=(("content-type", "text/event-stream"),))
            ),
            HttpStreamChunkEvent(b"data: eager\n\n"),
            HttpStreamCompleteEvent(),
        ]
    )
    seen: list[Message[object]] = []

    node = from_sse(graph, "https://example.test/events", stream_driver=driver, runner=runner)
    with node.subscribe(seen.append):
        pass

    assert runner.tasks[0].cancelled is True
    assert data_values(seen) == [SseEvent(data="eager")]
    assert seen[-1] == ControlMessage("COMPLETE")
