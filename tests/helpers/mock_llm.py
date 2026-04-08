"""Scenario-scripted mock LLM adapter for harness and AI pattern testing.

Detects the calling stage from prompt content and returns scripted responses.
Records all calls for assertions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class MockCall:
    """A single recorded call to the mock adapter."""

    stage: str
    messages: list[dict[str, str]]
    response: dict[str, str]


@dataclass
class StageScript:
    """Per-stage response script. Cycles through responses on each call."""

    responses: list[Any]


@dataclass
class MockScript:
    """Configuration for :func:`mock_llm`."""

    stages: dict[str, StageScript] | None = None
    fallback: Any = None


# ---------------------------------------------------------------------------
# Stage detection
# ---------------------------------------------------------------------------

_STAGE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("execute", ["implementation agent", "produce a fix"]),
    ("verify", ["qa reviewer", "verify whether"]),
    ("triage", ["triage classifier", "intake item"]),
]


def _detect_stage(text: str, custom_stages: dict[str, StageScript] | None = None) -> str:
    lower = text.lower()
    for stage, keywords in _STAGE_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return stage
    if custom_stages:
        for stage in custom_stages:
            if stage.lower() in lower:
                return stage
    return "unknown"


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


class MockLLMAdapter:
    """Extended LLM adapter with call recording and inspection."""

    def __init__(self, script: MockScript | None = None) -> None:
        self._script = script or MockScript()
        self.calls: list[MockCall] = []
        self.stage_counts: dict[str, int] = {}
        self._stage_indices: dict[str, int] = {}

    def _get_response(self, stage: str) -> Any:
        stages = self._script.stages
        if not stages or stage not in stages:
            return self._script.fallback or {}
        stage_script = stages[stage]
        idx = self._stage_indices.get(stage, 0)
        responses = stage_script.responses
        response = responses[min(idx, len(responses) - 1)]
        self._stage_indices[stage] = idx + 1
        return response

    async def invoke(self, messages: list[dict[str, str]]) -> dict[str, str]:
        text = " ".join(m.get("content", "") for m in messages)
        stage = _detect_stage(text, self._script.stages)
        self.stage_counts[stage] = self.stage_counts.get(stage, 0) + 1
        response_data = self._get_response(stage)
        content = response_data if isinstance(response_data, str) else json.dumps(response_data)
        response = {"content": content}
        self.calls.append(MockCall(stage=stage, messages=messages, response=response))
        return response

    async def stream(self, messages: list[dict[str, str]]) -> Any:
        async def _gen():
            yield "mock stream"
        return _gen()

    def calls_for(self, stage: str) -> list[MockCall]:
        return [c for c in self.calls if c.stage == stage]

    def reset(self) -> None:
        self.calls.clear()
        self.stage_counts.clear()
        self._stage_indices.clear()


def mock_llm(script: MockScript | None = None) -> MockLLMAdapter:
    """Create a scenario-scripted mock LLM adapter."""
    return MockLLMAdapter(script)
