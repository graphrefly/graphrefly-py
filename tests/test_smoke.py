"""Smoke tests for package import and baseline metadata."""

from __future__ import annotations

import graphrefly


def test_package_imports() -> None:
    assert graphrefly.__version__ == "0.1.0"
