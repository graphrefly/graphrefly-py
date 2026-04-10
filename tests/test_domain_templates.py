"""Tests for domain templates (Phase 8.2).

Mirrors graphrefly-ts/src/__tests__/patterns/domain-templates.test.ts.
"""

from __future__ import annotations

from graphrefly.core import node
from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import state
from graphrefly.patterns.domain_templates import (
    AnomalyResult,
    ContentModerationGraphOptions,
    DataQualityGraphOptions,
    ExtractedIssue,
    IssueTrackerGraphOptions,
    ModerationResult,
    ObservabilityBranch,
    ObservabilityGraphOptions,
    ValidationResult,
    content_moderation_graph,
    data_quality_graph,
    issue_tracker_graph,
    observability_graph,
)

# ---------------------------------------------------------------------------
# observability_graph
# ---------------------------------------------------------------------------


class TestObservabilityGraph:
    def test_creates_graph_with_well_known_nodes(self) -> None:
        source = state(None)
        g = observability_graph("obs", ObservabilityGraphOptions(source=source))
        desc = g.describe()

        assert desc["name"] == "obs"
        assert "source" in desc["nodes"]
        assert "correlate" in desc["nodes"]
        assert "slo_value" in desc["nodes"]
        assert "slo_verified" in desc["nodes"]
        assert "alerts" in desc["nodes"]
        assert "output" in desc["nodes"]

    def test_stratifies_into_default_branches(self) -> None:
        source = state(None)
        g = observability_graph("obs", ObservabilityGraphOptions(source=source))
        desc = g.describe()

        assert "stratify::branch/errors" in desc["nodes"]
        assert "stratify::branch/traces" in desc["nodes"]
        assert "stratify::branch/metrics" in desc["nodes"]

    def test_accepts_custom_branches(self) -> None:
        source = state(None)
        g = observability_graph(
            "obs",
            ObservabilityGraphOptions(
                source=source,
                branches=[
                    ObservabilityBranch(
                        "logs",
                        lambda v: isinstance(v, dict) and v.get("type") == "log",
                    ),
                ],
            ),
        )
        desc = g.describe()
        assert "stratify::branch/logs" in desc["nodes"]

    def test_slo_check_defaults_to_pass(self) -> None:
        source = node()
        g = observability_graph("obs", ObservabilityGraphOptions(source=source))

        slo = g.node("slo_verified")
        slo.subscribe(lambda _: None)
        output = g.node("output")
        output.subscribe(lambda _: None)

        source.down([(MessageType.DATA, {"type": "error", "msg": "test"})])
        result = slo.get()
        assert result == {"pass": True}

    def test_feedback_wires_up_for_slo_failures(self) -> None:
        source = state(None)
        g = observability_graph(
            "obs",
            ObservabilityGraphOptions(
                source=source,
                slo_check=lambda _v: {"pass": False, "reason": "latency"},
                max_feedback_iterations=2,
            ),
        )
        desc = g.describe()
        assert "feedback_reentry" in desc["nodes"]
        assert "feedback_condition" in desc["nodes"]

    def test_graph_is_destroyable(self) -> None:
        source = state(None)
        g = observability_graph("obs", ObservabilityGraphOptions(source=source))
        g.destroy()  # should not raise


# ---------------------------------------------------------------------------
# issue_tracker_graph
# ---------------------------------------------------------------------------


class TestIssueTrackerGraph:
    def test_creates_graph_with_well_known_nodes(self) -> None:
        source = state(None)
        g = issue_tracker_graph("issues", IssueTrackerGraphOptions(source=source))
        desc = g.describe()

        assert desc["name"] == "issues"
        assert "source" in desc["nodes"]
        assert "extract" in desc["nodes"]
        assert "verify" in desc["nodes"]
        assert "known_patterns" in desc["nodes"]
        assert "regression" in desc["nodes"]
        assert "priority" in desc["nodes"]
        assert "output" in desc["nodes"]

    def test_extracts_structured_issues(self) -> None:
        source = state(None)
        g = issue_tracker_graph(
            "issues",
            IssueTrackerGraphOptions(
                source=source,
                extract=lambda raw: ExtractedIssue(
                    id="issue-1",
                    title=str(raw),
                    severity=3,
                    source="test",
                    raw=raw,
                ),
            ),
        )

        extract = g.node("extract")
        extract.subscribe(lambda _: None)
        output = g.node("output")
        output.subscribe(lambda _: None)

        source.down([(MessageType.DATA, "found a bug")])
        issue = extract.get()
        assert isinstance(issue, ExtractedIssue)
        assert issue.id == "issue-1"
        assert issue.severity == 3

    def test_verify_defaults_to_valid(self) -> None:
        source = state(None)
        g = issue_tracker_graph("issues", IssueTrackerGraphOptions(source=source))

        verify = g.node("verify")
        verify.subscribe(lambda _: None)
        output = g.node("output")
        output.subscribe(lambda _: None)

        source.down([(MessageType.DATA, "test finding")])
        result = verify.get()
        assert isinstance(result, dict)
        assert result["verification"] == {"valid": True}

    def test_regression_detection_with_custom_fn(self) -> None:
        source = state(None)
        g = issue_tracker_graph(
            "issues",
            IssueTrackerGraphOptions(
                source=source,
                detect_regression=lambda issue, _known: {"regression": issue.severity > 2},
            ),
        )

        regression = g.node("regression")
        regression.subscribe(lambda _: None)
        output = g.node("output")
        output.subscribe(lambda _: None)

        source.down([(MessageType.DATA, "critical bug")])
        result = regression.get()
        assert isinstance(result, dict)
        assert "regression" in result


# ---------------------------------------------------------------------------
# content_moderation_graph
# ---------------------------------------------------------------------------


class TestContentModerationGraph:
    def test_creates_graph_with_well_known_nodes(self) -> None:
        source = state(None)
        g = content_moderation_graph("moderation", ContentModerationGraphOptions(source=source))
        desc = g.describe()

        assert desc["name"] == "moderation"
        assert "source" in desc["nodes"]
        assert "classify" in desc["nodes"]
        assert "review_queue" in desc["nodes"]
        assert "policy" in desc["nodes"]
        assert "priority" in desc["nodes"]
        assert "output" in desc["nodes"]

    def test_stratifies_into_safe_review_block(self) -> None:
        source = state(None)
        g = content_moderation_graph("moderation", ContentModerationGraphOptions(source=source))
        desc = g.describe()

        assert "stratify::branch/safe" in desc["nodes"]
        assert "stratify::branch/review" in desc["nodes"]
        assert "stratify::branch/block" in desc["nodes"]

    def test_classify_defaults_to_review(self) -> None:
        source = state(None)
        g = content_moderation_graph("moderation", ContentModerationGraphOptions(source=source))

        classify = g.node("classify")
        classify.subscribe(lambda _: None)
        output = g.node("output")
        output.subscribe(lambda _: None)

        source.down([(MessageType.DATA, "some content")])
        result = classify.get()
        assert isinstance(result, ModerationResult)
        assert result.label == "review"
        assert result.confidence == 0.5

    def test_policy_is_writable_state(self) -> None:
        source = state(None)
        g = content_moderation_graph("moderation", ContentModerationGraphOptions(source=source))

        policy = g.node("policy")
        policy.down([(MessageType.DATA, {"block_profanity": True})])
        assert policy.get() == {"block_profanity": True}


# ---------------------------------------------------------------------------
# data_quality_graph
# ---------------------------------------------------------------------------


class TestDataQualityGraph:
    def test_creates_graph_with_well_known_nodes(self) -> None:
        source = state(None)
        g = data_quality_graph("dq", DataQualityGraphOptions(source=source))
        desc = g.describe()

        assert desc["name"] == "dq"
        assert "source" in desc["nodes"]
        assert "validate" in desc["nodes"]
        assert "anomaly" in desc["nodes"]
        assert "baseline" in desc["nodes"]
        assert "drift" in desc["nodes"]
        assert "remediate" in desc["nodes"]
        assert "output" in desc["nodes"]

    def test_validate_defaults_to_valid(self) -> None:
        source = state(None)
        g = data_quality_graph("dq", DataQualityGraphOptions(source=source))

        validate = g.node("validate")
        validate.subscribe(lambda _: None)
        output = g.node("output")
        output.subscribe(lambda _: None)

        source.down([(MessageType.DATA, {"id": 1, "name": "test"})])
        result = validate.get()
        assert isinstance(result, ValidationResult)
        assert result.valid is True
        assert result.errors == []

    def test_custom_validation_catches_invalid_records(self) -> None:
        source = state(None)
        g = data_quality_graph(
            "dq",
            DataQualityGraphOptions(
                source=source,
                validate=lambda record: ValidationResult(
                    valid=bool(record.get("name") if isinstance(record, dict) else False),
                    errors=(
                        []
                        if (isinstance(record, dict) and record.get("name"))
                        else ["missing name"]
                    ),
                    record=record,
                ),
            ),
        )

        output = g.node("output")
        output.subscribe(lambda _: None)
        validate = g.node("validate")
        validate.subscribe(lambda _: None)

        source.down([(MessageType.DATA, {"id": 1})])
        result = validate.get()
        assert isinstance(result, ValidationResult)
        assert result.valid is False
        assert "missing name" in result.errors

    def test_anomaly_detection_works(self) -> None:
        source = state(None)
        g = data_quality_graph(
            "dq",
            DataQualityGraphOptions(
                source=source,
                detect_anomaly=lambda record: AnomalyResult(
                    anomaly=record.get("value", 0) > 100 if isinstance(record, dict) else False,
                    score=0.9 if (isinstance(record, dict) and record.get("value", 0) > 100) else 0,
                    record=record,
                ),
            ),
        )

        output = g.node("output")
        output.subscribe(lambda _: None)
        anomaly = g.node("anomaly")
        anomaly.subscribe(lambda _: None)

        source.down([(MessageType.DATA, {"value": 999})])
        result = anomaly.get()
        assert isinstance(result, AnomalyResult)
        assert result.anomaly is True
        assert result.score == 0.9

    def test_output_combines_all_quality_checks(self) -> None:
        source = node()  # SENTINEL: no push on subscribe
        g = data_quality_graph("dq", DataQualityGraphOptions(source=source))

        output = g.node("output")
        output.subscribe(lambda _: None)

        source.down([(MessageType.DATA, {"id": 1, "name": "test"})])
        result = output.get()
        assert isinstance(result, dict)
        assert "validation" in result
        assert "anomaly" in result
        assert "drift" in result
        assert "remediation" in result

    def test_feedback_wires_anomalies_to_validation_rules(self) -> None:
        source = state(None)
        g = data_quality_graph("dq", DataQualityGraphOptions(source=source))
        desc = g.describe()

        assert "validation_rules" in desc["nodes"]
        assert "feedback_condition" in desc["nodes"]

    def test_graph_is_destroyable(self) -> None:
        source = state(None)
        g = data_quality_graph("dq", DataQualityGraphOptions(source=source))
        g.destroy()  # should not raise
