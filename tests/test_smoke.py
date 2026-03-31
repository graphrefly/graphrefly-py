"""Smoke tests for package import and baseline metadata."""

from __future__ import annotations

import graphrefly


def test_package_imports() -> None:
    assert graphrefly.__version__ == "0.1.0"
    assert hasattr(graphrefly, "patterns")
    assert hasattr(graphrefly.patterns, "orchestration")
    assert hasattr(graphrefly.patterns, "messaging")
    assert hasattr(graphrefly.patterns, "memory")
    assert hasattr(graphrefly.patterns.orchestration, "pipeline")
    assert hasattr(graphrefly.patterns.orchestration, "task")
    assert hasattr(graphrefly.patterns.orchestration, "branch")
    assert hasattr(graphrefly.patterns.orchestration, "gate")
    assert hasattr(graphrefly.patterns.orchestration, "approval")
    assert hasattr(graphrefly.patterns.orchestration, "for_each")
    assert hasattr(graphrefly.patterns.orchestration, "join")
    assert hasattr(graphrefly.patterns.orchestration, "loop")
    assert hasattr(graphrefly.patterns.orchestration, "sub_pipeline")
    assert hasattr(graphrefly.patterns.orchestration, "sensor")
    assert hasattr(graphrefly.patterns.orchestration, "wait")
    assert hasattr(graphrefly.patterns.orchestration, "on_failure")
    assert hasattr(graphrefly.patterns.messaging, "topic")
    assert hasattr(graphrefly.patterns.messaging, "subscription")
    assert hasattr(graphrefly.patterns.messaging, "job_queue")
    assert hasattr(graphrefly.patterns.messaging, "job_flow")
    assert hasattr(graphrefly.patterns.messaging, "topic_bridge")
    assert hasattr(graphrefly.patterns.memory, "collection")
    assert hasattr(graphrefly.patterns.memory, "light_collection")
    assert hasattr(graphrefly.patterns.memory, "vector_index")
    assert hasattr(graphrefly.patterns.memory, "knowledge_graph")
    assert hasattr(graphrefly.patterns.memory, "decay")
