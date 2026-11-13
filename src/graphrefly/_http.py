"""HTTP and SSE sources for the Python facade, over caller-supplied drivers."""

from __future__ import annotations

import codecs
from collections.abc import AsyncIterable, Awaitable, Iterable
from dataclasses import dataclass
from inspect import isawaitable
from typing import Literal, Protocol, cast

from graphrefly._async import (
    AsyncJobFactory,
    AsyncRunner,
    _AsyncCompletionGate,
    _AsyncJob,
    _close_async_stream,
    _QueuedCompletionGate,
    _runner_reentry_queue,
    _settle_async_error,
    _start_async,
    _validate_runner,
)
from graphrefly._facade import (
    Graph,
    Node,
)
from graphrefly.exceptions import (
    GraphReflyRuntimeError,
    GraphReflyValueError,
)

_SSE_PARSER_MAX_BUFFER_BYTES = 64 * 1024


_SSE_RETRY_MAX = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """Host-driver HTTP request material for Python network source adapters."""

    method: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Host-driver HTTP response material emitted by `from_http`."""

    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class HttpStreamHead:
    """HTTP response head material for streaming source drivers."""

    status: int
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class HttpStreamHeadEvent:
    """Streaming driver event carrying the HTTP response head."""

    head: HttpStreamHead
    kind: Literal["Head"] = "Head"


@dataclass(frozen=True, slots=True)
class HttpStreamChunkEvent:
    """Streaming driver event carrying a bytes chunk."""

    chunk: bytes
    kind: Literal["Chunk"] = "Chunk"


@dataclass(frozen=True, slots=True)
class HttpStreamErrorEvent:
    """Streaming driver event carrying a host-side stream error."""

    error: BaseException
    kind: Literal["Error"] = "Error"


@dataclass(frozen=True, slots=True)
class HttpStreamCompleteEvent:
    """Streaming driver event marking normal stream completion."""

    kind: Literal["Complete"] = "Complete"


@dataclass(frozen=True, slots=True)
class SseEvent:
    """Parsed server-sent event DATA emitted by `from_sse`."""

    data: str
    event: str | None = None
    id: str | None = None
    retry_ms: int | None = None


type HttpRequestLike = str | HttpRequest


type HttpStreamDriverEvent = (
    HttpStreamHeadEvent | HttpStreamChunkEvent | HttpStreamErrorEvent | HttpStreamCompleteEvent
)


class LocalHttpDriver(Protocol):
    """Host-owned one-shot HTTP driver used by `from_http`."""

    def request(self, request: HttpRequest) -> Awaitable[HttpResponse]:
        """Start one HTTP request and resolve to an `HttpResponse`."""
        ...


class LocalHttpStreamDriver(Protocol):
    """Host-owned streaming HTTP driver used by `from_sse`."""

    def stream(self, request: HttpRequest) -> AsyncIterable[HttpStreamDriverEvent]:
        """Start one HTTP stream and yield normalized stream events."""
        ...


def from_http(
    graph: Graph,
    request_or_url: HttpRequestLike,
    *,
    driver: LocalHttpDriver | None = None,
    runner: AsyncRunner | None = None,
    name: str | None = None,
) -> Node[HttpResponse]:
    """Create a source from one host-supplied HTTP request driver call per activation.

    Its default local name is ``fromHttp`` with a numeric suffix when occupied.
    """

    request = _normalize_http_request(request_or_url)
    _validate_network_source_args(
        graph, runner, driver, driver_method="request", driver_name="LocalHttpDriver"
    )
    assert runner is not None
    assert driver is not None
    http = driver
    reentry_queue = _runner_reentry_queue(graph, runner)

    def body(gate: _AsyncCompletionGate | _QueuedCompletionGate, job: _AsyncJob) -> AsyncJobFactory:
        async def run() -> None:
            try:
                if not job.active:
                    return
                response = http.request(request)
                if not isawaitable(response):
                    msg = "LocalHttpDriver.request(request) must return an awaitable"
                    raise GraphReflyRuntimeError(msg)
                gate.resolve(_normalize_http_response(await response), job.can_apply_completion)
            except BaseException as error:
                _settle_async_error(gate, job, error)
            finally:
                job.finish()

        return run

    return cast(
        "Node[HttpResponse]",
        graph._source(
            graph._module_name(name, "fromHttp"),
            lambda ctx: _start_async(graph, ctx, runner, reentry_queue, body),
        ),
    )


def from_sse(
    graph: Graph,
    request_or_url: HttpRequestLike,
    *,
    stream_driver: LocalHttpStreamDriver | None = None,
    runner: AsyncRunner | None = None,
    name: str | None = None,
) -> Node[SseEvent]:
    """Create an SSE source over a host-supplied HTTP byte-stream driver.

    Its default local name is ``fromSse`` with a numeric suffix when occupied.
    """

    request = _sse_request_to_http_stream_request(_normalize_http_request(request_or_url))
    _validate_network_source_args(
        graph, runner, stream_driver, driver_method="stream", driver_name="LocalHttpStreamDriver"
    )
    assert runner is not None
    assert stream_driver is not None
    streams = stream_driver
    reentry_queue = _runner_reentry_queue(graph, runner)

    def body(gate: _AsyncCompletionGate | _QueuedCompletionGate, job: _AsyncJob) -> AsyncJobFactory:
        async def run() -> None:
            parser = _SseParser()
            saw_head = False
            stream_iter: object | None = None
            try:
                if not job.active:
                    return
                stream = streams.stream(request)
                if not hasattr(stream, "__aiter__"):
                    msg = "LocalHttpStreamDriver.stream(request) must return an async iterable"
                    raise GraphReflyRuntimeError(msg)
                stream_iter = stream.__aiter__()
                try:
                    async for raw_event in cast("AsyncIterable[object]", stream_iter):
                        if not job.active:
                            return
                        event = _normalize_http_stream_event(raw_event)
                        if isinstance(event, HttpStreamHeadEvent):
                            if saw_head:
                                raise GraphReflyRuntimeError(
                                    "from_sse: http stream emitted duplicate response head"
                                )
                            saw_head = True
                            _validate_sse_head(event.head)
                            continue
                        if isinstance(event, HttpStreamChunkEvent):
                            if len(event.chunk) == 0:
                                continue
                            if not saw_head:
                                raise GraphReflyRuntimeError(
                                    "from_sse: http stream chunk arrived before response head"
                                )
                            if len(event.chunk) > _SSE_PARSER_MAX_BUFFER_BYTES:
                                raise _SseParserError("from_sse: parser overflow", [])
                            try:
                                parsed_events = parser.push(event.chunk)
                            except _SseParserError as error:
                                for sse_event in error.events:
                                    gate.emit(sse_event, job.can_apply_completion)
                                raise error
                            for sse_event in parsed_events:
                                gate.emit(sse_event, job.can_apply_completion)
                            continue
                        if isinstance(event, HttpStreamErrorEvent):
                            if job.active:
                                gate.error(event.error, job.can_apply_completion)
                            return
                        if not saw_head:
                            raise GraphReflyRuntimeError(
                                "from_sse: http stream completed before response head"
                            )
                        try:
                            parsed_events = parser.complete()
                        except _SseParserError as error:
                            for sse_event in error.events:
                                gate.emit(sse_event, job.can_apply_completion)
                            raise error
                        for sse_event in parsed_events:
                            gate.emit(sse_event, job.can_apply_completion)
                        gate.complete(job.can_apply_completion)
                        return
                finally:
                    await _close_async_stream(stream_iter)
                raise GraphReflyRuntimeError("from_sse: http stream ended without Complete")
            except BaseException as error:
                _settle_async_error(gate, job, error)
            finally:
                job.finish()

        return run

    return cast(
        "Node[SseEvent]",
        graph._source(
            graph._module_name(name, "fromSse"),
            lambda ctx: _start_async(graph, ctx, runner, reentry_queue, body),
        ),
    )


def _normalize_http_request(request_or_url: HttpRequestLike) -> HttpRequest:
    if isinstance(request_or_url, str):
        if request_or_url == "":
            raise GraphReflyValueError("HTTP request url must be a non-empty str")
        return HttpRequest(method="GET", url=request_or_url)
    if not isinstance(request_or_url, HttpRequest):
        msg = "HTTP request must be a URL str or graphrefly.HttpRequest"
        raise GraphReflyValueError(msg)
    method = _normalize_non_empty_str("HTTP request method", request_or_url.method)
    url = _normalize_non_empty_str("HTTP request url", request_or_url.url)
    return HttpRequest(
        method=method,
        url=url,
        headers=_normalize_headers(request_or_url.headers),
        body=_normalize_bytes("HTTP request body", request_or_url.body),
    )


def _normalize_http_response(response: object) -> HttpResponse:
    if not isinstance(response, HttpResponse):
        msg = "LocalHttpDriver.request(request) must resolve to graphrefly.HttpResponse"
        raise GraphReflyRuntimeError(msg)
    status = _normalize_status("HTTP response status", response.status)
    return HttpResponse(
        status=status,
        headers=_normalize_headers(response.headers),
        body=_normalize_bytes("HTTP response body", response.body),
    )


def _normalize_http_stream_event(event: object) -> HttpStreamDriverEvent:
    if isinstance(event, HttpStreamHeadEvent):
        return HttpStreamHeadEvent(_normalize_http_stream_head(event.head))
    if isinstance(event, HttpStreamHead):
        return HttpStreamHeadEvent(_normalize_http_stream_head(event))
    if isinstance(event, HttpStreamChunkEvent):
        return HttpStreamChunkEvent(_normalize_bytes("HTTP stream chunk", event.chunk))
    if isinstance(event, HttpStreamErrorEvent):
        if not isinstance(event.error, BaseException):
            msg = "HTTP stream Error event must carry a BaseException"
            raise GraphReflyRuntimeError(msg)
        return event
    if isinstance(event, HttpStreamCompleteEvent):
        return event
    msg = "HTTP stream driver events must be Head, Chunk, Error, or Complete"
    raise GraphReflyRuntimeError(msg)


def _normalize_http_stream_head(head: object) -> HttpStreamHead:
    if not isinstance(head, HttpStreamHead):
        msg = "HTTP stream Head event must carry graphrefly.HttpStreamHead"
        raise GraphReflyRuntimeError(msg)
    return HttpStreamHead(
        status=_normalize_status("HTTP stream status", head.status),
        headers=_normalize_headers(head.headers),
    )


def _sse_request_to_http_stream_request(request: HttpRequest) -> HttpRequest:
    headers = list(request.headers)
    if not any(key.lower() == "accept" for key, _value in headers):
        headers.append(("Accept", "text/event-stream"))
    return HttpRequest(
        method="GET",
        url=request.url,
        headers=tuple(headers),
        body=b"",
    )


def _validate_sse_head(head: HttpStreamHead) -> None:
    if not 200 <= head.status <= 299:
        raise GraphReflyRuntimeError(f"from_sse: unacceptable http status {head.status}")
    content_type = next(
        (value for key, value in head.headers if key.lower() == "content-type"),
        None,
    )
    if content_type is None:
        raise GraphReflyRuntimeError("from_sse: missing text/event-stream content-type")
    media_type = content_type.split(";", 1)[0].strip()
    if media_type.lower() != "text/event-stream":
        raise GraphReflyRuntimeError(f"from_sse: unacceptable content-type {content_type}")


def _normalize_headers(headers: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    normalized: list[tuple[str, str]] = []
    for item in headers:
        if not isinstance(item, tuple) or len(item) != 2:
            msg = "HTTP headers must be (name, value) string pairs"
            raise GraphReflyValueError(msg)
        key, value = item
        if not isinstance(key, str) or key == "":
            raise GraphReflyValueError("HTTP header names must be non-empty strings")
        if not isinstance(value, str):
            raise GraphReflyValueError("HTTP header values must be strings")
        normalized.append((key, value))
    return tuple(normalized)


def _normalize_bytes(label: str, value: object) -> bytes:
    if isinstance(value, bytes):
        return bytes(value)
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    raise GraphReflyValueError(f"{label} must be bytes")


def _normalize_non_empty_str(label: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise GraphReflyValueError(f"{label} must be a non-empty str")
    return value


def _normalize_status(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 100 or value > 999:
        raise GraphReflyValueError(f"{label} must be an HTTP status int")
    return value


def _validate_network_source_args(
    graph: Graph,
    runner: AsyncRunner | None,
    driver: object | None,
    *,
    driver_method: str,
    driver_name: str,
) -> None:
    graph._check_thread()
    if runner is None:
        msg = "network source adapters require an explicit AsyncRunner"
        raise GraphReflyValueError(msg)
    _validate_runner(runner)
    if driver is None:
        raise GraphReflyValueError(f"{driver_name} is required")
    if not callable(getattr(driver, driver_method, None)):
        raise GraphReflyValueError(f"{driver_name} must provide {driver_method}(request)")


class _SseParserError(GraphReflyRuntimeError):
    def __init__(self, message: str, events: list[SseEvent]) -> None:
        self.events = events
        super().__init__(message)


class _SseParser:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._line = ""
        self._line_bytes = 0
        self._drop_next_lf = False
        self._event: str | None = None
        self._data_lines: list[str] = []
        self._data_bytes = 0
        self._id: str | None = None
        self._retry_ms: int | None = None

    def push(self, chunk: bytes) -> list[SseEvent]:
        try:
            text = self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError as error:
            raise _SseParserError("from_sse: invalid utf-8 in event stream", []) from error
        return self._push_text(text)

    def complete(self) -> list[SseEvent]:
        events: list[SseEvent] = []
        try:
            tail = self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as error:
            raise _SseParserError("from_sse: invalid utf-8 in event stream", events) from error
        events.extend(self._push_text(tail))
        if self._line:
            try:
                self._finish_line_into(events)
            except _SseParserError as error:
                raise _SseParserError(str(error), events + error.events) from error
        dispatched = self._dispatch_event()
        if dispatched is not None:
            events.append(dispatched)
        return events

    def _push_text(self, text: str) -> list[SseEvent]:
        events: list[SseEvent] = []
        for char in text:
            if self._drop_next_lf:
                self._drop_next_lf = False
                if char == "\n":
                    continue
            if char == "\r":
                try:
                    self._finish_line_into(events)
                except _SseParserError as error:
                    raise _SseParserError(str(error), events + error.events) from error
                self._drop_next_lf = True
            elif char == "\n":
                try:
                    self._finish_line_into(events)
                except _SseParserError as error:
                    raise _SseParserError(str(error), events + error.events) from error
            else:
                self._line += char
                self._line_bytes += len(char.encode("utf-8"))
                if self._line_bytes > _SSE_PARSER_MAX_BUFFER_BYTES:
                    raise _SseParserError("from_sse: parser overflow", events)
        return events

    def _finish_line_into(self, events: list[SseEvent]) -> None:
        event = self._process_line(self._line)
        self._line = ""
        self._line_bytes = 0
        if event is not None:
            events.append(event)

    def _process_line(self, line: str) -> SseEvent | None:
        if line == "":
            return self._dispatch_event()
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            added = len(value.encode("utf-8")) + (1 if self._data_lines else 0)
            if self._data_bytes + added > _SSE_PARSER_MAX_BUFFER_BYTES:
                raise _SseParserError("from_sse: parser overflow", [])
            self._data_bytes += added
            self._data_lines.append(value)
        elif field == "event":
            self._event = value
        elif field == "id":
            self._id = value
        elif field == "retry" and value.isdecimal() and len(value) <= 20:
            retry_ms = int(value)
            if retry_ms <= _SSE_RETRY_MAX:
                self._retry_ms = retry_ms
        return None

    def _dispatch_event(self) -> SseEvent | None:
        if not self._data_lines:
            self._clear_event()
            return None
        event = SseEvent(
            event=self._event,
            data="\n".join(self._data_lines),
            id=self._id,
            retry_ms=self._retry_ms,
        )
        self._clear_event()
        return event

    def _clear_event(self) -> None:
        self._event = None
        self._data_lines = []
        self._data_bytes = 0
        self._id = None
        self._retry_ms = None
