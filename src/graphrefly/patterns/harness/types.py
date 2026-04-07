"""Harness wiring types (roadmap §9.0).

Shared types for the reactive collaboration loop: intake, triage, queue,
gate, execute, verify, reflect. These types are intentionally domain-agnostic
— the harness loop is not specific to eval workflows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------

IntakeSource = Literal[
    "eval", "test", "human", "code-change", "hypothesis", "parity"
]

Severity = Literal["critical", "high", "medium", "low"]

RootCause = Literal[
    "composition", "missing-fn", "bad-docs", "schema-gap", "regression", "unknown"
]

Intervention = Literal[
    "template", "catalog-fn", "docs", "wrapper", "schema-change", "investigate"
]

QueueRoute = Literal["auto-fix", "needs-decision", "investigation", "backlog"]


@dataclass(frozen=True, slots=True)
class IntakeItem:
    """An item entering the harness loop via the INTAKE stage."""

    source: IntakeSource
    summary: str
    evidence: str
    affects_areas: tuple[str, ...]
    affects_eval_tasks: tuple[str, ...] | None = None
    severity: Severity | None = None
    related_to: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# Triage output
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TriagedItem:
    """Output of the TRIAGE stage — enriched intake item with classification."""

    source: IntakeSource
    summary: str
    evidence: str
    affects_areas: tuple[str, ...]
    root_cause: RootCause
    intervention: Intervention
    route: QueueRoute
    priority: float
    affects_eval_tasks: tuple[str, ...] | None = None
    severity: Severity | None = None
    related_to: tuple[str, ...] | None = None
    triage_reasoning: str | None = None


# ---------------------------------------------------------------------------
# Strategy model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategyEntry:
    """Effectiveness record for a root_cause→intervention pair."""

    root_cause: RootCause
    intervention: Intervention
    attempts: int
    successes: int
    success_rate: float


StrategyKey = str  # f"{root_cause}→{intervention}"


def strategy_key(root_cause: RootCause, intervention: Intervention) -> StrategyKey:
    return f"{root_cause}→{intervention}"


# ---------------------------------------------------------------------------
# Execution & verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Result of the EXECUTE stage."""

    item: TriagedItem
    outcome: Literal["success", "failure", "partial"]
    detail: str
    retry_count: int = 0


ErrorClass = Literal["self-correctable", "structural"]
ErrorClassifier = "Callable[[ExecutionResult], ErrorClass]"


def default_error_classifier(result: ExecutionResult) -> ErrorClass:
    """Default error classifier: parse/config errors are self-correctable."""
    d = result.detail.lower()
    if any(
        kw in d for kw in ("parse", "json", "config", "validation", "syntax")
    ):
        return "self-correctable"
    return "structural"


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Result of the VERIFY stage."""

    item: TriagedItem
    execution: ExecutionResult
    verified: bool
    findings: tuple[str, ...]
    error_class: ErrorClass | None = None


# ---------------------------------------------------------------------------
# Priority scoring
# ---------------------------------------------------------------------------

DEFAULT_SEVERITY_WEIGHTS: dict[str, float] = {
    "critical": 100.0,
    "high": 70.0,
    "medium": 40.0,
    "low": 10.0,
}

DEFAULT_DECAY_RATE: float = math.log(2) / (7 * 24 * 3600)


@dataclass(frozen=True, slots=True)
class PrioritySignals:
    """Configurable signals for priority scoring."""

    severity_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SEVERITY_WEIGHTS))
    decay_rate: float = DEFAULT_DECAY_RATE
    effectiveness_threshold: float = 0.7
    effectiveness_boost: float = 15.0


# ---------------------------------------------------------------------------
# Harness loop configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueueConfig:
    """Per-queue configuration in the harness loop."""

    gated: bool
    max_pending: int | float = float("inf")
    start_open: bool = False


DEFAULT_QUEUE_CONFIGS: dict[str, QueueConfig] = {
    "auto-fix": QueueConfig(gated=False),
    "needs-decision": QueueConfig(gated=True),
    "investigation": QueueConfig(gated=True),
    "backlog": QueueConfig(gated=False, start_open=False),
}

QUEUE_NAMES: tuple[str, ...] = (
    "auto-fix",
    "needs-decision",
    "investigation",
    "backlog",
)
