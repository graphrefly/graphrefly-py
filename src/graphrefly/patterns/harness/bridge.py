"""Harness bridge factories (roadmap §9.0).

Intake bridges, eval source wrapper, before/after comparison,
affected-task filter, code-change bridge, and notification effect.
All are compositions of existing primitives — no new abstractions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from graphrefly.core.sugar import derived, effect, pipe, state
from graphrefly.extra.sources import from_any
from graphrefly.extra.tier2 import switch_map

if TYPE_CHECKING:
    from collections.abc import Callable

    from graphrefly.core.node import NodeImpl
    from graphrefly.patterns.messaging import TopicGraph

from .types import IntakeItem, Severity

# ---------------------------------------------------------------------------
# Generic intake bridge
# ---------------------------------------------------------------------------


def create_intake_bridge(
    source: NodeImpl[Any],
    intake_topic: TopicGraph,
    parser: Any,
    *,
    name: str | None = None,
) -> NodeImpl[Any]:
    """Generic source→intake bridge factory.

    Watches a source node for new values, passes each through a user-supplied
    ``parser`` that produces zero or more ``IntakeItem`` instances, and publishes
    them to the given intake topic.

    This is the generalized pattern behind :func:`eval_intake_bridge`. Use it
    for CI results, test failures, Slack messages, monitoring alerts, or any
    domain where structured results should flow into a harness loop.

    Args:
        source: Reactive node emitting domain-specific data.
        intake_topic: TopicGraph to publish IntakeItem entries to.
        parser: ``(value: T) -> list[IntakeItem]``. Return empty list to skip.
        name: Optional name for the effect node.

    Returns:
        The effect node (for lifecycle management).
    """

    def _bridge(deps: list[Any], _actions: Any) -> None:
        value = deps[0]
        if value is None:
            return
        items = parser(value)
        for item in items:
            intake_topic.publish(item)

    return effect([source], _bridge, name=name or "intake-bridge")


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


# ---------------------------------------------------------------------------
# Composition A: Eval-driven improvement loop
# ---------------------------------------------------------------------------


def eval_source(
    trigger: NodeImpl[Any],
    runner: Callable[[], Any],
) -> NodeImpl[Any]:
    """Wrap any eval runner as a reactive producer node.

    When ``trigger`` emits, calls ``runner()`` and emits the result downstream.
    Uses ``switch_map`` + ``from_any`` — the async boundary stays in the source
    layer (spec §5.10). A new trigger cancels any in-flight run.

    Args:
        trigger: Any node; each new DATA emission fires the runner.
        runner:  Zero-argument callable returning an EvalResult,
                 or a coroutine / Awaitable of one.

    Returns:
        Node emitting the runner's return value on each trigger.
    """

    def _project(value: Any) -> Any:
        return from_any(runner())

    return pipe(trigger, switch_map(_project))


# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvalTaskDelta:
    """Per-task delta from :func:`before_after_compare`."""

    task_id: str
    before: bool
    after: bool
    score_diff: int | None = None  # after_passes - before_passes; None if no scores


@dataclass(frozen=True, slots=True)
class EvalDelta:
    """Output of :func:`before_after_compare`."""

    new_failures: tuple[str, ...]
    """Task IDs that newly fail in ``after`` (were passing in ``before``)."""

    resolved: tuple[str, ...]
    """Task IDs that now pass in ``after`` (were failing in ``before``)."""

    task_deltas: tuple[EvalTaskDelta, ...]
    """Full per-task breakdown."""

    overall_improved: bool
    """True when net resolutions > net failures."""


def before_after_compare(
    before: NodeImpl[Any],
    after: NodeImpl[Any],
) -> NodeImpl[EvalDelta]:
    """Derived node that computes before/after eval deltas.

    Pure computation: no LLM, no async. Compares per-task validity and
    pass counts between two ``EvalResult`` snapshots.

    Args:
        before: Node holding the baseline eval result.
        after:  Node holding the new eval result.

    Returns:
        Node emitting an :class:`EvalDelta` whenever either input changes.
    """

    def _compute(dep_values: list[Any], _actions: Any) -> EvalDelta:
        b_res = dep_values[0]
        a_res = dep_values[1]

        before_map: dict[str, Any] = {t.task_id: t for t in b_res.tasks}
        after_map: dict[str, Any] = {t.task_id: t for t in a_res.tasks}
        all_ids = sorted(set(before_map) | set(after_map))

        task_deltas: list[EvalTaskDelta] = []
        new_failures: list[str] = []
        resolved: list[str] = []

        for tid in all_ids:
            bt = before_map.get(tid)
            at = after_map.get(tid)
            before_valid = bt.valid if bt is not None else False
            after_valid = at.valid if at is not None else False

            before_passes = (
                sum(1 for s in bt.judge_scores if getattr(s, "pass_", False))
                if bt is not None and bt.judge_scores
                else None
            )
            after_passes = (
                sum(1 for s in at.judge_scores if getattr(s, "pass_", False))
                if at is not None and at.judge_scores
                else None
            )
            score_diff = (
                after_passes - before_passes
                if before_passes is not None and after_passes is not None
                else None
            )

            task_deltas.append(
                EvalTaskDelta(
                    task_id=tid,
                    before=before_valid,
                    after=after_valid,
                    score_diff=score_diff,
                )
            )
            if before_valid and not after_valid:
                new_failures.append(tid)
            if not before_valid and after_valid:
                resolved.append(tid)

        return EvalDelta(
            new_failures=tuple(new_failures),
            resolved=tuple(resolved),
            task_deltas=tuple(task_deltas),
            overall_improved=len(resolved) > len(new_failures),
        )

    return derived([before, after], _compute, name="eval-delta")


# ---------------------------------------------------------------------------


def affected_task_filter(
    issues: NodeImpl[Any],
    full_task_set: NodeImpl[Any] | tuple[str, ...] | list[str] | None = None,
) -> NodeImpl[list[str]]:
    """Derived node that selects which eval task IDs to re-run.

    Collects ``affects_eval_tasks`` from all triaged items, deduplicates,
    then optionally intersects with ``full_task_set``. Returns a sorted list.

    Args:
        issues:        Node holding the current list of triaged items.
        full_task_set: Optional node (or plain tuple/list) of all known task
                       IDs. When provided, output is the intersection.

    Returns:
        Node emitting a sorted list of affected task IDs.
    """
    if full_task_set is None:
        task_set_node = None
    elif isinstance(full_task_set, (tuple, list)):
        task_set_node = state(tuple(full_task_set))
    else:
        task_set_node = full_task_set

    deps: list[NodeImpl[Any]] = [issues]
    if task_set_node is not None:
        deps.append(task_set_node)

    def _compute(dep_values: list[Any], _actions: Any) -> list[str]:
        items = dep_values[0] or ()
        all_ids: set[str] | None = (
            set(dep_values[1]) if task_set_node is not None else None
        )
        affected: set[str] = set()
        for item in items:
            for tid in (getattr(item, "affects_eval_tasks", None) or ()):
                if all_ids is None or tid in all_ids:
                    affected.add(tid)
        return sorted(affected)

    return derived(deps, _compute, name="affected-task-filter")


# ---------------------------------------------------------------------------
# Composition D: Quality gate (CI/CD)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LintError:
    file: str
    line: int
    col: int
    rule: str
    message: str


@dataclass(frozen=True, slots=True)
class TestFailure:
    test_id: str
    file: str
    message: str


@dataclass(frozen=True, slots=True)
class CodeChange:
    """Structured code-change / CI event."""

    files: tuple[str, ...]
    lint_errors: tuple[LintError, ...] = field(default_factory=tuple)
    test_failures: tuple[TestFailure, ...] = field(default_factory=tuple)


def code_change_bridge(
    source: NodeImpl[Any],
    intake_topic: TopicGraph,
    parser: Callable[[Any], list[IntakeItem]] | None = None,
    *,
    name: str | None = None,
    default_severity: Severity = "high",
) -> NodeImpl[Any]:
    """Intake bridge for code-change / CI events.

    Watches a source node for :class:`CodeChange` events and publishes one
    ``IntakeItem`` per lint error and per test failure to the intake topic.
    Pass a custom ``parser`` to override the default mapping.

    Args:
        source:           Node emitting CodeChange events.
        intake_topic:     TopicGraph to publish IntakeItem entries to.
        parser:           Optional custom parser (overrides default).
        name:             Name for the effect node.
        default_severity: Default severity for generated items.

    Returns:
        The effect node (for lifecycle management).
    """

    def _default_parser(change: CodeChange) -> list[IntakeItem]:
        items: list[IntakeItem] = []
        for err in change.lint_errors:
            items.append(
                IntakeItem(
                    source="code-change",
                    summary=f"Lint: {err.rule} in {err.file}:{err.line}",
                    evidence=err.message,
                    affects_areas=(err.file,),
                    severity=default_severity,
                )
            )
        for fail in change.test_failures:
            items.append(
                IntakeItem(
                    source="test",
                    summary=f"Test failure: {fail.test_id}",
                    evidence=fail.message,
                    affects_areas=(fail.file,),
                    affects_eval_tasks=(fail.test_id,),
                    severity=default_severity,
                )
            )
        return items

    resolve = parser if parser is not None else _default_parser

    def _bridge(deps: list[Any], _actions: Any) -> None:
        change = deps[0]
        if change is None:
            return
        for item in resolve(change):
            intake_topic.publish(item)

    return effect([source], _bridge, name=name or "code-change-bridge")


# ---------------------------------------------------------------------------


def notify_effect(
    topic: TopicGraph,
    transport: Callable[[Any], Any],
    *,
    name: str | None = None,
) -> NodeImpl[Any]:
    """Effect node that sends each new topic entry to an external channel.

    The ``transport`` callable is invoked for every item published to ``topic``.
    Async transports (coroutines / awaitables) are fire-and-forget — results
    do not feed back into the graph.

    Args:
        topic:     TopicGraph whose latest entry triggers the notification.
        transport: Called with each new item. May be sync or async.
        name:      Name for the effect node.

    Returns:
        The effect node (for lifecycle management).
    """

    def _notify(deps: list[Any], _actions: Any) -> None:
        item = deps[0]
        if item is None:
            return
        # transport is a side effect (webhook, Slack, etc.). Async results are
        # fire-and-forget — they don't feed back into the graph.
        transport(item)

    return effect([topic.latest], _notify, name=name or "notify-effect")
