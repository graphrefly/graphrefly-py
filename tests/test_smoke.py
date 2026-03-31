"""Smoke tests for package import and baseline metadata."""

from __future__ import annotations

import graphrefly


def test_package_imports() -> None:
    assert graphrefly.__version__ == "0.1.0"
    assert hasattr(graphrefly, "patterns")
    assert hasattr(graphrefly.patterns, "orchestration")
    assert hasattr(graphrefly.patterns.orchestration, "pipeline")
    assert hasattr(graphrefly.patterns.orchestration, "task")
    assert hasattr(graphrefly.patterns.orchestration, "branch")
    assert hasattr(graphrefly.patterns.orchestration, "gate")
    assert hasattr(graphrefly.patterns.orchestration, "approval")
