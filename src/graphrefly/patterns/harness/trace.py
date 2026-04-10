"""harness_trace() — pipeline stage tracer (roadmap § Inspection Tool Consolidation).

Attaches ``observe(format="json")`` to harness stage nodes for full pipeline
visibility with stage labels and elapsed timestamps from the call site.

Supports two output modes:

- **String logger** (default): rendered lines to ``print`` or a custom sink.
- **Structured events**: programmatic ``TraceEvent`` list for test assertions
  and tooling. Access via ``handle.events``.

Supports configurable detail levels (``"summary"``, ``"standard"``, ``"full"``)
to control output verbosity without composing different tool calls.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from graphrefly.core.clock import monotonic_ns

if TYPE_CHECKING:
    from collections.abc import Callable

    from graphrefly.graph.graph import ObserveResult
    from graphrefly.patterns.harness.loop import HarnessGraph

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

TraceEventType = Literal["data", "error", "complete"]
TraceDetail = Literal["summary", "standard", "full"]
"""Detail levels: ``"summary"`` (stage + elapsed), ``"standard"`` (+ data preview),
``"full"`` (+ raw data in events). Default is ``"summary"``."""


@dataclass(slots=True)
class TraceEvent:
    """A single structured trace event."""

    elapsed: float
    """Elapsed seconds since trace was created."""

    stage: str
    """Pipeline stage label (INTAKE, TRIAGE, QUEUE, GATE, EXECUTE, VERIFY, STRATEGY)."""

    type: TraceEventType
    """Event type."""

    data: Any = None
    """Data payload (present for 'data' and 'error' events). Omitted at 'summary' detail."""

    summary: str | None = None
    """Human-readable summary. Present at 'standard' and 'full' detail."""


class HarnessTraceHandle:
    """Disposable handle returned by :func:`harness_trace`.

    Provides both ``dispose()`` to stop tracing and ``events`` for
    programmatic access to structured trace events.
    """

    __slots__ = ("_disposers", "_events")

    def __init__(
        self,
        disposers: list[Callable[[], None]],
        events: list[TraceEvent],
    ) -> None:
        self._disposers = disposers
        self._events = events

    @property
    def events(self) -> list[TraceEvent]:
        """Structured trace events collected since creation.

        Plain list — no subscription needed (COMPOSITION-GUIDE §1: avoid
        lazy-activation friction for inspection tools). Populated reactively
        via observe().
        """
        return self._events

    def dispose(self) -> None:
        disposers, self._disposers = self._disposers, []
        for fn in disposers:
            fn()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarize(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (str, int, float, bool)):
        s = str(value)
        return s[:80] + "…" if len(s) > 80 else s
    try:
        s = json.dumps(value)
        return s[:120] + "…" if len(s) > 120 else s
    except Exception:
        return str(value)[:80]


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def harness_trace(
    harness: HarnessGraph,
    logger: Callable[[str], None] | None = None,
    *,
    detail: TraceDetail = "summary",
) -> HarnessTraceHandle:
    """Wire ``observe(format="json")`` to all accessible harness stages.

    Emits stage-labelled lines with elapsed timestamps relative to the
    :func:`harness_trace` invocation time — matching TS ``harnessTrace`` output
    style. Returns a :class:`HarnessTraceHandle` whose ``dispose()`` detaches
    all listeners and ``events`` provides structured access.

    **Detail levels:**

    - ``"summary"`` — stage + elapsed only. Minimal overhead.
    - ``"standard"`` (default) — stage + elapsed + truncated data preview.
    - ``"full"`` — stage + elapsed + full raw data in events.

    Args:
        harness: A :class:`HarnessGraph` returned by :func:`harness_loop`.
        logger: Optional sink for rendered lines. Defaults to ``print``.
            Pass explicit callable to capture; omit for console; the structured
            ``handle.events`` is always populated regardless.
        detail: Detail level for both string and structured output.
            Default ``"summary"`` — stage + elapsed, minimal overhead.
    """
    sink: Callable[[str], None] | None = logger or print
    start_ns = monotonic_ns()
    disposers: list[Callable[[], None]] = []
    events: list[TraceEvent] = []

    def elapsed_secs() -> float:
        return (monotonic_ns() - start_ns) / 1e9

    def elapsed_str() -> str:
        return f"{elapsed_secs():.3f}"

    def _record_event(
        stage: str, etype: TraceEventType, raw_data: Any,
    ) -> None:
        e = elapsed_secs()
        ev = TraceEvent(elapsed=e, stage=stage, type=etype)

        if detail != "summary":
            ev.summary = _summarize(raw_data)
        if detail == "full":
            ev.data = raw_data

        events.append(ev)

    def _make_logger(stage: str) -> Callable[[str, dict[str, Any]], None]:
        label = stage.upper().ljust(9)

        def _log(line: str, event: dict[str, Any]) -> None:
            etype = event.get("type")
            if etype == "data":
                _record_event(stage.upper(), "data", event.get("data"))
                if sink:
                    if detail == "summary":
                        sink(f"[{elapsed_str()}s] {label} ←")
                    else:
                        data_str = f" {_summarize(event.get('data'))}" if "data" in event else ""
                        sink(f"[{elapsed_str()}s] {label} ←{data_str}")
            elif etype == "error":
                _record_event(stage.upper(), "error", event.get("data"))
                if sink:
                    err_str = f" {_summarize(event.get('data'))}" if "data" in event else ""
                    sink(f"[{elapsed_str()}s] {label} ✗{err_str}")
            elif etype == "complete":
                _record_event(stage.upper(), "complete", None)
                if sink:
                    sink(f"[{elapsed_str()}s] {label} ■ complete")

        return _log

    def _wire(graph: Any, path: str, stage: str) -> None:
        result = cast(
            "ObserveResult",
            graph.observe(
                path,
                format="json",
                include_types=["data", "error", "complete"],
                logger=_make_logger(stage),
            ),
        )
        disposers.append(result.dispose)

    try:
        # Stage nodes registered on the harness graph by harness_loop
        # (COMPOSITION-GUIDE §5: wire sinks/observers before sources emit)
        for stage, path in [
            ("intake", "intake::latest"),
            ("triage", "triage"),
            ("execute", "execute"),
            ("verify", "verify-results::latest"),
            ("strategy", "strategy"),
        ]:
            with contextlib.suppress(Exception):
                _wire(harness, path, stage)

        # Queue outputs
        for route, topic in harness.queues.items():
            with contextlib.suppress(Exception):
                _wire(topic, "latest", f"queue/{route}")

        # Gate outputs
        for route in harness.gates:
            gate_path = f"gates::{route}/gate"
            with contextlib.suppress(Exception):
                _wire(harness, gate_path, f"gate/{route}")

    except Exception:
        for fn in disposers:
            fn()
        raise

    return HarnessTraceHandle(disposers, events)


__all__ = ["HarnessTraceHandle", "TraceDetail", "TraceEvent", "TraceEventType", "harness_trace"]
