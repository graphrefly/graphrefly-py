"""Harness wiring (roadmap §9.0).

Reactive collaboration loop: static-topology, flowing data.
Composes orchestration (gate), AI (prompt_node), reduction (scorer/stratify),
and messaging (TopicGraph/bridge) into a 7-stage loop.
"""

from graphrefly.patterns.harness.bridge import (
    EvalJudgeScore,
    EvalResult,
    EvalTaskResult,
    eval_intake_bridge,
)
from graphrefly.patterns.harness.loop import HarnessGraph, harness_loop
from graphrefly.patterns.harness.profile import HarnessProfileResult, harness_profile
from graphrefly.patterns.harness.strategy import (
    StrategyModelBundle,
    StrategySnapshot,
    priority_score,
    strategy_model,
)
from graphrefly.patterns.harness.types import (
    DEFAULT_DECAY_RATE,
    DEFAULT_QUEUE_CONFIGS,
    DEFAULT_SEVERITY_WEIGHTS,
    QUEUE_NAMES,
    ErrorClass,
    ErrorClassifier,
    ExecuteOutput,
    ExecutionResult,
    IntakeItem,
    Intervention,
    PrioritySignals,
    QueueConfig,
    QueueRoute,
    RootCause,
    Severity,
    StrategyEntry,
    StrategyKey,
    TriagedItem,
    VerifyResult,
    default_error_classifier,
    strategy_key,
)

__all__ = [
    # types
    "DEFAULT_DECAY_RATE",
    "DEFAULT_QUEUE_CONFIGS",
    "DEFAULT_SEVERITY_WEIGHTS",
    "QUEUE_NAMES",
    "ErrorClass",
    "ErrorClassifier",
    "ExecuteOutput",
    "ExecutionResult",
    "IntakeItem",
    "Intervention",
    "PrioritySignals",
    "QueueConfig",
    "QueueRoute",
    "RootCause",
    "Severity",
    "StrategyEntry",
    "StrategyKey",
    "TriagedItem",
    "VerifyResult",
    "default_error_classifier",
    "strategy_key",
    # strategy
    "StrategyModelBundle",
    "StrategySnapshot",
    "priority_score",
    "strategy_model",
    # bridge
    "EvalJudgeScore",
    "EvalResult",
    "EvalTaskResult",
    "eval_intake_bridge",
    # loop
    "HarnessGraph",
    "harness_loop",
    # profile
    "HarnessProfileResult",
    "harness_profile",
]
