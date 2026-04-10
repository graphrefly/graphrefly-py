"""Domain templates (roadmap §8.2).

Opinionated Graph factories for common "info → action" domains.
Each template wires up §8.1 reduction primitives (stratify, funnel, feedback,
budget_gate, scorer) with domain-specific stages. Users fork/extend by
accessing named nodes and swapping stages.

**Source injection (option B):** templates accept a ``source`` node, not a
hardcoded adapter. Pass ``from_otel(...)``, ``from_git_hook(...)``, or a test
``state()`` — the topology is the same.
"""

from __future__ import annotations

import contextlib as _contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from graphrefly.core.node import NodeImpl  # noqa: TC001
from graphrefly.core.protocol import MessageType, batch
from graphrefly.core.sugar import derived, effect, state
from graphrefly.extra.data_structures import reactive_log
from graphrefly.graph.graph import Graph
from graphrefly.patterns._internal import domain_meta
from graphrefly.patterns.reduction import (
    StratifyRule,
    feedback,
    scorer,
    stratify,
)

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def _base_meta(kind: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return domain_meta("domain_template", kind, extra)


def _is_tagged(value: Any, tag: str) -> bool:
    """Check if a value has a ``type`` or ``kind`` tag matching *tag*."""
    if not isinstance(value, dict):
        return False
    return value.get("type") == tag or value.get("kind") == tag


# ---------------------------------------------------------------------------
# 1. observability_graph
# ---------------------------------------------------------------------------


@dataclass
class ObservabilityBranch:
    """Stratification branch config for observability signals."""

    name: str
    classify: Callable[[Any], bool]


@dataclass
class ObservabilityGraphOptions:
    """Options for :func:`observability_graph`."""

    source: NodeImpl[Any]
    branches: list[ObservabilityBranch] | None = None
    correlate: Callable[[list[Any]], Any] | None = None
    slo_check: Callable[[Any], Any] | None = None
    weights: list[float] | None = None
    max_feedback_iterations: int = 5


def observability_graph(
    name: str,
    opts: ObservabilityGraphOptions,
) -> Graph:
    """OTel ingest → stratified reduction → correlation → SLO verification →
    alert prioritization → output.

    Well-known node names:
    - ``"source"`` — injected signal source
    - ``"stratify::branch/<name>"`` — per-branch classification
    - ``"correlate"`` — cross-branch correlation
    - ``"slo_value"``, ``"slo_verified"`` — SLO verification pair
    - ``"alerts"`` — scored, prioritized output
    - ``"output"`` — final output
    """
    g = Graph(name)

    # --- Source ---
    g.add("source", opts.source)

    # --- Stratify ---
    default_branches = [
        ObservabilityBranch("errors", lambda v: _is_tagged(v, "error")),
        ObservabilityBranch("traces", lambda v: _is_tagged(v, "trace")),
        ObservabilityBranch("metrics", lambda v: _is_tagged(v, "metric")),
    ]
    branches = opts.branches or default_branches
    rules = [StratifyRule(b.name, b.classify) for b in branches]
    strat = stratify("stratify", opts.source, rules)
    g.mount("stratify", strat)

    # --- Correlate ---
    # Wrap each branch in a derived with initial=None so every branch has
    # a seed value at subscribe time — this lets the correlate wave reach its
    # first-run gate even when the classifier only routes to one branch.
    branch_nodes: list[NodeImpl[Any]] = []
    for b in branches:
        try:
            raw = g.resolve(f"stratify::branch/{b.name}")
            branch_nodes.append(derived([raw], lambda vals, _a: vals[0], initial=None))
        except Exception:  # noqa: BLE001
            branch_nodes.append(state(None))

    correlate_fn = opts.correlate or (lambda vals: vals)
    correlate_node = derived(
        branch_nodes,
        lambda vals, _a: correlate_fn(vals),
        meta=_base_meta("observability", {"stage": "correlate"}),
    )
    g.add("correlate", correlate_node)
    for b in branches:
        with _contextlib.suppress(Exception):
            g.connect(f"stratify::branch/{b.name}", "correlate")

    # --- SLO verification ---
    slo_check_fn = opts.slo_check or (lambda _v: {"pass": True})
    slo_value = derived(
        [correlate_node],
        lambda vals, _a: vals[0],
        meta=_base_meta("observability", {"stage": "slo_value"}),
    )
    slo_verified = derived(
        [slo_value],
        lambda vals, _a: slo_check_fn(vals[0]),
        meta=_base_meta("observability", {"stage": "slo_verified"}),
    )
    g.add("slo_value", slo_value)
    g.add("slo_verified", slo_verified)
    g.connect("correlate", "slo_value")
    g.connect("slo_value", "slo_verified")

    # --- Alert scorer ---
    weight_values = opts.weights or [1.0] * len(branches)
    signal_nodes = [
        derived([bn], lambda vals, _a: 1 if vals[0] is not None else 0) for bn in branch_nodes
    ]
    weight_nodes = [state(w) for w in weight_values]
    for i, (sn, wn) in enumerate(zip(signal_nodes, weight_nodes, strict=True)):
        g.add(f"__signal_{i}", sn)
        g.add(f"__weight_{i}", wn)

    alerts = scorer(signal_nodes, weight_nodes)
    g.add("alerts", alerts)

    # --- Output ---
    output_node = derived(
        [alerts, slo_verified],
        lambda vals, _a: {"scored": vals[0], "slo": vals[1]},
        meta=_base_meta("observability", {"stage": "output"}),
    )
    g.add("output", output_node)
    g.connect("alerts", "output")
    g.connect("slo_verified", "output")

    # --- Feedback (false-positive learning) ---
    fb_reentry = state(
        None,
        meta=_base_meta("observability", {"stage": "feedback_reentry"}),
    )
    g.add("feedback_reentry", fb_reentry)

    def _fb_cond(vals: list[Any], _a: Any) -> Any:
        result = vals[0]
        if isinstance(result, dict) and result.get("pass") is False:
            return result
        return None

    fb_condition = derived(
        [slo_verified],
        _fb_cond,
        meta=_base_meta("observability", {"stage": "feedback_condition"}),
    )
    g.add("feedback_condition", fb_condition)
    g.connect("slo_verified", "feedback_condition")
    feedback(
        g, "feedback_condition", "feedback_reentry", max_iterations=opts.max_feedback_iterations
    )

    return g


# ---------------------------------------------------------------------------
# 2. issue_tracker_graph
# ---------------------------------------------------------------------------


@dataclass
class ExtractedIssue:
    """A structured issue extracted from raw findings."""

    id: str
    title: str
    severity: int
    source: str
    raw: Any


@dataclass
class IssueTrackerGraphOptions:
    """Options for :func:`issue_tracker_graph`."""

    source: NodeImpl[Any]
    extract: Callable[[Any], ExtractedIssue] | None = None
    verify: Callable[[ExtractedIssue], Any] | None = None
    detect_regression: Callable[[ExtractedIssue, Any], Any] | None = None
    max_feedback_iterations: int = 3


def issue_tracker_graph(
    name: str,
    opts: IssueTrackerGraphOptions,
) -> Graph:
    """Findings ingest → extraction → verification → regression detection →
    distillation → prioritized queue.

    Well-known node names:
    - ``"source"`` — injected findings source
    - ``"extract"`` — structured issue extraction
    - ``"verify"`` — issue verification
    - ``"known_patterns"`` — accumulated known issue patterns (state)
    - ``"regression"`` — regression detection
    - ``"priority"`` — severity-based prioritization
    - ``"output"`` — final prioritized output
    """
    g = Graph(name)

    # --- Source ---
    g.add("source", opts.source)

    # --- Extract ---
    _issue_counter = 0

    def _default_extract(raw: Any) -> ExtractedIssue:
        nonlocal _issue_counter
        _issue_counter += 1
        return ExtractedIssue(
            id=f"issue-{_issue_counter}",
            title=str(raw),
            severity=1,
            source="unknown",
            raw=raw,
        )

    extract_fn = opts.extract or _default_extract
    extract_node = derived(
        [opts.source],
        lambda vals, _a: extract_fn(vals[0]),
        meta=_base_meta("issue_tracker", {"stage": "extract"}),
    )
    g.add("extract", extract_node)
    g.connect("source", "extract")

    # --- Verify ---
    verify_fn = opts.verify or (lambda _issue: {"valid": True})
    verify_node = derived(
        [extract_node],
        lambda vals, _a: {"issue": vals[0], "verification": verify_fn(vals[0])},
        meta=_base_meta("issue_tracker", {"stage": "verify"}),
    )
    g.add("verify", verify_node)
    g.connect("extract", "verify")

    # --- Known patterns (memory / distillation state) ---
    known_patterns = state(
        [],
        meta=_base_meta("issue_tracker", {"stage": "known_patterns"}),
    )
    g.add("known_patterns", known_patterns)

    # --- Regression detection ---
    detect_fn = opts.detect_regression or (lambda _issue, _known: {"regression": False})
    regression_node = derived(
        [extract_node, known_patterns],
        lambda vals, _a: {"issue": vals[0], "regression": detect_fn(vals[0], vals[1])},
        meta=_base_meta("issue_tracker", {"stage": "regression"}),
    )
    g.add("regression", regression_node)
    g.connect("extract", "regression")
    g.connect("known_patterns", "regression")

    # --- Priority scoring ---
    severity_signal = derived(
        [extract_node],
        lambda vals, _a: getattr(vals[0], "severity", 0) if vals[0] is not None else 0,
    )
    regression_signal = derived(
        [regression_node],
        lambda vals, _a: 2 if (isinstance(vals[0], dict) and vals[0].get("regression")) else 0,
    )
    g.add("__severity_signal", severity_signal)
    g.add("__regression_signal", regression_signal)

    severity_weight = state(1.0)
    regression_weight = state(1.5)
    g.add("__severity_weight", severity_weight)
    g.add("__regression_weight", regression_weight)

    priority = scorer(
        [severity_signal, regression_signal],
        [severity_weight, regression_weight],
    )
    g.add("priority", priority)

    # --- Output ---
    output_node = derived(
        [verify_node, regression_node, priority],
        lambda vals, _a: {"verified": vals[0], "regression": vals[1], "priority": vals[2]},
        meta=_base_meta("issue_tracker", {"stage": "output"}),
    )
    g.add("output", output_node)
    g.connect("verify", "output")
    g.connect("regression", "output")
    g.connect("priority", "output")

    # --- Feedback (re-scan on verification failure) ---
    fb_reentry = state(
        None,
        meta=_base_meta("issue_tracker", {"stage": "feedback_reentry"}),
    )
    g.add("feedback_reentry", fb_reentry)

    def _fb_cond(vals: list[Any], _a: Any) -> Any:
        result = vals[0]
        if isinstance(result, dict):
            v = result.get("verification")
            if isinstance(v, dict) and v.get("valid") is False:
                return result
        return None

    fb_condition = derived(
        [verify_node],
        _fb_cond,
        meta=_base_meta("issue_tracker", {"stage": "feedback_condition"}),
    )
    g.add("feedback_condition", fb_condition)
    g.connect("verify", "feedback_condition")
    feedback(
        g, "feedback_condition", "feedback_reentry", max_iterations=opts.max_feedback_iterations
    )

    return g


# ---------------------------------------------------------------------------
# 3. content_moderation_graph
# ---------------------------------------------------------------------------


@dataclass
class ModerationResult:
    """Classification result from LLM moderation."""

    label: str  # "safe" | "review" | "block"
    confidence: float
    reason: str | None = None
    original: Any = None


@dataclass
class ContentModerationGraphOptions:
    """Options for :func:`content_moderation_graph`."""

    source: NodeImpl[Any]
    classify: Callable[[Any], ModerationResult] | None = None
    system_prompt: str | None = None
    weights: tuple[float, float, float] = (0.1, 1.0, 2.0)
    max_feedback_iterations: int = 5
    max_queue_size: int | None = None


def content_moderation_graph(
    name: str,
    opts: ContentModerationGraphOptions,
) -> Graph:
    """Content ingest → classification → stratified routing (safe/review/block) →
    human review queue → scorer → feedback (false positives → policy refinement) → output.

    Well-known node names:
    - ``"source"`` — content ingest
    - ``"classify"`` — rule-based classification
    - ``"stratify::branch/safe"``, ``"stratify::branch/review"``, ``"stratify::branch/block"``
    - ``"review_queue"`` — state node for human review items
    - ``"priority"`` — scored priority output
    - ``"policy"`` — writable state for policy refinement
    - ``"output"`` — final moderation output
    """
    g = Graph(name)

    # --- Source ---
    g.add("source", opts.source)

    # --- Classify ---
    default_classify = lambda content: ModerationResult(  # noqa: E731
        label="review",
        confidence=0.5,
        original=content,
    )
    classify_fn = opts.classify or default_classify
    classify_node = derived(
        [opts.source],
        lambda vals, _a: classify_fn(vals[0]),
        meta=_base_meta("content_moderation", {"stage": "classify"}),
    )
    g.add("classify", classify_node)
    g.connect("source", "classify")

    # --- Stratify (safe / review / block) ---
    strat = stratify(
        "stratify",
        classify_node,
        [
            StratifyRule("safe", lambda v: getattr(v, "label", None) == "safe"),
            StratifyRule("review", lambda v: getattr(v, "label", None) == "review"),
            StratifyRule("block", lambda v: getattr(v, "label", None) == "block"),
        ],
    )
    g.mount("stratify", strat)

    # --- Review queue (reactiveLog — O(1) append, bounded) ---
    review_log = reactive_log(
        [],
        max_size=opts.max_queue_size,
        name="review_queue",
    )
    g.add("review_queue", review_log.entries)

    # Bridge review branch → review queue accumulator
    try:
        review_branch = g.resolve("stratify::branch/review")
    except Exception:  # noqa: BLE001
        review_branch = state(None)
        g.add("__review_fallback", review_branch)

    def _accumulate(vals: list[Any], _a: Any) -> None:
        item = vals[0]
        if item is not None:
            review_log.append(item)

    review_accumulator = effect([review_branch], _accumulate)
    g.add("__review_accumulator", review_accumulator)

    # --- Policy state (human/LLM writable) ---
    policy = state(
        {},
        meta=_base_meta(
            "content_moderation",
            {
                "stage": "policy",
                "access": "both",
                "description": "Moderation policy rules — updated via feedback",
            },
        ),
    )
    g.add("policy", policy)

    # --- Priority scorer ---
    weights = opts.weights
    confidence_signal = derived(
        [classify_node],
        lambda vals, _a: getattr(vals[0], "confidence", 0) if vals[0] is not None else 0,
    )
    severity_signal = derived(
        [classify_node],
        lambda vals, _a: (
            (
                weights[2]
                if getattr(vals[0], "label", None) == "block"
                else weights[1]
                if getattr(vals[0], "label", None) == "review"
                else weights[0]
            )
            if vals[0] is not None
            else 0
        ),
    )
    g.add("__confidence_signal", confidence_signal)
    g.add("__severity_signal", severity_signal)

    w_confidence = state(1.0)
    w_severity = state(1.0)
    g.add("__w_confidence", w_confidence)
    g.add("__w_severity", w_severity)

    priority = scorer(
        [confidence_signal, severity_signal],
        [w_confidence, w_severity],
    )
    g.add("priority", priority)

    # --- Output ---
    output_node = derived(
        [classify_node, priority],
        lambda vals, _a: {"classification": vals[0], "priority": vals[1]},
        meta=_base_meta("content_moderation", {"stage": "output"}),
    )
    g.add("output", output_node)
    g.connect("classify", "output")
    g.connect("priority", "output")

    # --- Feedback (false positive → policy refinement) ---
    def _fb_cond(vals: list[Any], _a: Any) -> Any:
        snap = vals[0]
        entries = getattr(snap, "value", None) if snap is not None else None
        if entries and len(entries) > 0:
            latest = entries[-1]
            if isinstance(latest, dict) and latest.get("false_positive"):
                return latest
        return None

    fb_condition = derived(
        [review_log.entries, policy],
        _fb_cond,
        meta=_base_meta("content_moderation", {"stage": "feedback_condition"}),
    )
    g.add("feedback_condition", fb_condition)
    feedback(g, "feedback_condition", "policy", max_iterations=opts.max_feedback_iterations)

    return g


# ---------------------------------------------------------------------------
# 4. data_quality_graph
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Schema validation result."""

    valid: bool
    errors: list[str]
    record: Any


@dataclass
class AnomalyResult:
    """Anomaly detection result."""

    anomaly: bool
    score: float
    record: Any
    detail: str | None = None


@dataclass
class DataQualityGraphOptions:
    """Options for :func:`data_quality_graph`."""

    source: NodeImpl[Any]
    validate: Callable[[Any], ValidationResult] | None = None
    detect_anomaly: Callable[[Any], AnomalyResult] | None = None
    detect_drift: Callable[[Any, Any], Any] | None = None
    suggest: Callable[[dict[str, Any]], Any] | None = None
    max_feedback_iterations: int = 3


def data_quality_graph(
    name: str,
    opts: DataQualityGraphOptions,
) -> Graph:
    """Data ingest → schema validation → anomaly detection → drift alerting →
    auto-remediation suggestions → output.

    Well-known node names:
    - ``"source"`` — data ingest
    - ``"validate"`` — schema validation
    - ``"anomaly"`` — anomaly detection
    - ``"baseline"`` — rolling baseline state
    - ``"drift"`` — drift detection
    - ``"remediate"`` — auto-remediation suggestions
    - ``"output"`` — combined quality report
    """
    g = Graph(name)

    # --- Source ---
    g.add("source", opts.source)

    # --- Schema validation ---
    default_validate = lambda record: ValidationResult(  # noqa: E731
        valid=True,
        errors=[],
        record=record,
    )
    validate_fn = opts.validate or default_validate
    validate_node = derived(
        [opts.source],
        lambda vals, _a: validate_fn(vals[0]) if vals[0] is not None else None,
        meta=_base_meta("data_quality", {"stage": "validate"}),
    )
    g.add("validate", validate_node)
    g.connect("source", "validate")

    # --- Anomaly detection ---
    default_anomaly = lambda record: AnomalyResult(  # noqa: E731
        anomaly=False,
        score=0,
        record=record,
    )
    detect_anomaly_fn = opts.detect_anomaly or default_anomaly
    anomaly_node = derived(
        [opts.source],
        lambda vals, _a: detect_anomaly_fn(vals[0]) if vals[0] is not None else None,
        meta=_base_meta("data_quality", {"stage": "anomaly"}),
    )
    g.add("anomaly", anomaly_node)
    g.connect("source", "anomaly")

    # --- Baseline (rolling state) ---
    baseline = state(
        None,
        meta=_base_meta(
            "data_quality",
            {
                "stage": "baseline",
                "description": "Rolling baseline for drift detection",
            },
        ),
    )
    g.add("baseline", baseline)

    # Update baseline on valid records
    def _update_baseline(vals: list[Any], _a: Any) -> None:
        result = vals[0]
        if result is not None and getattr(result, "valid", False):
            with batch():
                baseline.down([(MessageType.DATA, result.record)])

    baseline_updater = effect([validate_node], _update_baseline)
    g.add("__baseline_updater", baseline_updater)
    g.connect("validate", "__baseline_updater")

    # --- Drift detection ---
    detect_drift_fn = opts.detect_drift or (lambda _r, _b: {"drift": False})
    drift_node = derived(
        [opts.source, baseline],
        lambda vals, _a: detect_drift_fn(vals[0], vals[1]),
        meta=_base_meta("data_quality", {"stage": "drift"}),
    )
    g.add("drift", drift_node)
    g.connect("source", "drift")
    g.connect("baseline", "drift")

    # --- Remediation suggestions ---
    suggest_fn = opts.suggest or (lambda _r: None)
    remediate_node = derived(
        [validate_node, anomaly_node],
        lambda vals, _a: suggest_fn({"validation": vals[0], "anomaly": vals[1]}),
        meta=_base_meta("data_quality", {"stage": "remediate"}),
    )
    g.add("remediate", remediate_node)
    g.connect("validate", "remediate")
    g.connect("anomaly", "remediate")

    # --- Output ---
    output_node = derived(
        [validate_node, anomaly_node, drift_node, remediate_node],
        lambda vals, _a: {
            "validation": vals[0],
            "anomaly": vals[1],
            "drift": vals[2],
            "remediation": vals[3],
        },
        meta=_base_meta("data_quality", {"stage": "output"}),
    )
    g.add("output", output_node)
    g.connect("validate", "output")
    g.connect("anomaly", "output")
    g.connect("drift", "output")
    g.connect("remediate", "output")

    # --- Feedback (anomaly → validation rule refinement) ---
    validation_rules = state(
        [],
        meta=_base_meta("data_quality", {"stage": "validation_rules"}),
    )
    g.add("validation_rules", validation_rules)

    def _fb_cond(vals: list[Any], _a: Any) -> Any:
        a = vals[0]
        if a is not None and getattr(a, "anomaly", False):
            return a
        return None

    fb_condition = derived(
        [anomaly_node],
        _fb_cond,
        meta=_base_meta("data_quality", {"stage": "feedback_condition"}),
    )
    g.add("feedback_condition", fb_condition)
    g.connect("anomaly", "feedback_condition")
    feedback(
        g, "feedback_condition", "validation_rules", max_iterations=opts.max_feedback_iterations
    )

    return g
