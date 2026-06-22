"""Passive DATA-level issue envelopes for Python host values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DataIssue:
    """Reserved DATA payload for domain/material issues, not protocol ERROR."""

    code: str
    message: str
    severity: str = "issue"
    details: dict[str, Any] | None = None
