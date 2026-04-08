"""harness_trace() — pipeline stage tracer (roadmap § Inspection Tool Consolidation).

Attaches ``observe(format="json")`` to harness stage nodes for full pipeline
visibility with stage labels and elapsed timestamps from the call site.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any, cast

from graphrefly.core.clock import monotonic_ns

if TYPE_CHECKING:
    from collections.abc import Callable

    from graphrefly.graph.graph import ObserveResult
    from graphrefly.patterns.harness.loop import HarnessGraph


class HarnessTraceHandle:
    """Disposable handle returned by :func:`harness_trace`."""

    __slots__ = ("_disposers",)

    def __init__(self, disposers: list[Callable[[], None]]) -> None:
        self._disposers = disposers

    def dispose(self) -> None:
        disposers, self._disposers = self._disposers, []
        for fn in disposers:
            fn()


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


def harness_trace(
    harness: HarnessGraph,
    logger: Callable[[str], None] | None = None,
) -> HarnessTraceHandle:
    """Wire ``observe(format="json")`` to all accessible harness stages.

    Emits stage-labelled lines with elapsed timestamps relative to the
    :func:`harness_trace` invocation time — matching TS ``harnessTrace`` output
    style. Returns a :class:`HarnessTraceHandle` whose ``dispose()`` detaches
    all listeners.

    Args:
        harness: A :class:`HarnessGraph` returned by :func:`harness_loop`.
        logger: Optional sink for rendered lines. Defaults to ``print``.
    """
    sink = logger or print
    start_ns = monotonic_ns()
    disposers: list[Callable[[], None]] = []

    def elapsed() -> str:
        delta_ns = monotonic_ns() - start_ns
        return f"{delta_ns / 1e9:.3f}"

    def _make_logger(stage: str) -> Callable[[str, dict[str, Any]], None]:
        label = stage.upper().ljust(9)

        def _log(line: str, event: dict[str, Any]) -> None:
            etype = event.get("type")
            if etype == "data":
                data_str = f" {_summarize(event.get('data'))}" if "data" in event else ""
                sink(f"[{elapsed()}s] {label} ←{data_str}")
            elif etype == "error":
                err_str = f" {_summarize(event.get('data'))}" if "data" in event else ""
                sink(f"[{elapsed()}s] {label} ✗{err_str}")
            elif etype == "complete":
                sink(f"[{elapsed()}s] {label} ■ complete")

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

    return HarnessTraceHandle(disposers)


__all__ = ["HarnessTraceHandle", "harness_trace"]
