"""Eval→intake bridge (roadmap §9.0).

Effect node that parses eval results into IntakeItem instances and publishes
to an intake TopicGraph. Produces per-criterion findings, not per-task scores.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from graphrefly.core.sugar import effect

if TYPE_CHECKING:
    from graphrefly.core.node import NodeImpl
    from graphrefly.patterns.messaging import TopicGraph

from .types import IntakeItem, Severity

# ---------------------------------------------------------------------------
# Generic eval result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvalJudgeScore:
    claim: str
    pass_: bool  # 'pass' is a Python keyword
    reasoning: str


@dataclass(frozen=True, slots=True)
class EvalTaskResult:
    task_id: str
    valid: bool
    judge_scores: tuple[EvalJudgeScore, ...] | None = None


@dataclass(frozen=True, slots=True)
class EvalResult:
    """Minimal eval result shape accepted by the bridge."""

    run_id: str
    model: str
    tasks: tuple[EvalTaskResult, ...]


# ---------------------------------------------------------------------------
# Bridge factory
# ---------------------------------------------------------------------------


def eval_intake_bridge(
    eval_source: NodeImpl[Any],
    intake_topic: TopicGraph,
    *,
    name: str | None = None,
    default_severity: Severity = "medium",
) -> NodeImpl[Any]:
    """Create an effect node that watches an eval results source and publishes
    per-criterion findings to an intake topic.

    Each failing judge criterion produces a separate IntakeItem — not one
    item per task.

    Args:
        eval_source: Node emitting EvalResult (or list of EvalResult).
        intake_topic: TopicGraph to publish IntakeItem entries to.
        name: Optional name for the effect node.
        default_severity: Default severity for eval-sourced items.

    Returns:
        The effect node (for lifecycle management).
    """

    def _bridge(deps: list[Any], _actions: Any) -> None:
        results = deps[0]
        if results is None:
            return
        runs = results if isinstance(results, (list, tuple)) else [results]

        for run in runs:
            for task in run.tasks:
                scores = task.judge_scores or ()
                # Skip fully passing tasks
                if task.valid and all(getattr(s, "pass_", False) for s in scores):
                    continue

                # Task-level invalidity without judge scores
                if not task.valid and len(scores) == 0:
                    intake_topic.publish(
                        IntakeItem(
                            source="eval",
                            summary=f"Task {task.task_id} invalid (model: {run.model})",
                            evidence=f"Run {run.run_id}: task produced invalid output",
                            affects_areas=("graphspec",),
                            affects_eval_tasks=(task.task_id,),
                            severity=default_severity,
                        )
                    )
                    continue

                # Per-criterion findings
                for score in scores:
                    if getattr(score, "pass_", False):
                        continue
                    intake_topic.publish(
                        IntakeItem(
                            source="eval",
                            summary=f"{task.task_id}: {score.claim} (model: {run.model})",
                            evidence=score.reasoning,
                            affects_areas=("graphspec",),
                            affects_eval_tasks=(task.task_id,),
                            severity=default_severity,
                        )
                    )

    return effect([eval_source], _bridge)
