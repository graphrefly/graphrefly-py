"""Strategy model and priority scoring (roadmap §9.0).

Pure-computation derived nodes — no LLM, no async.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from graphrefly.core.clock import monotonic_ns
from graphrefly.core.sugar import derived
from graphrefly.extra.data_structures import reactive_map
from graphrefly.patterns.memory import decay

from .types import (
    DEFAULT_SEVERITY_WEIGHTS,
    Intervention,
    PrioritySignals,
    RootCause,
    StrategyEntry,
    StrategyKey,
    strategy_key,
)

if TYPE_CHECKING:
    from graphrefly.core.node import NodeImpl


# ---------------------------------------------------------------------------
# Strategy model
# ---------------------------------------------------------------------------

StrategySnapshot = dict[StrategyKey, StrategyEntry]


@dataclass(slots=True)
class StrategyModelBundle:
    """Bundle returned by :func:`strategy_model`."""

    node: NodeImpl[Any]
    """Reactive node — current strategy map."""

    _map: Any  # ReactiveMapBundle (avoid exposing internal type)
    _unsub: list[Any]  # [unsub_fn] or [] — mutable container so frozen-safe

    def _read(self, key: StrategyKey) -> StrategyEntry | None:
        """Read a value from the underlying reactive map."""
        result: StrategyEntry | None = self._map.get(key)
        return result

    def record(self, root_cause: RootCause, intervention: Intervention, success: bool) -> None:
        """Record a completed issue (success or failure)."""
        key = strategy_key(root_cause, intervention)
        existing = self._read(key)
        attempts = (existing.attempts if existing else 0) + 1
        successes = (existing.successes if existing else 0) + (1 if success else 0)
        self._map.set(
            key,
            StrategyEntry(
                root_cause=root_cause,
                intervention=intervention,
                attempts=attempts,
                successes=successes,
                success_rate=successes / attempts,
            ),
        )

    def lookup(self, root_cause: RootCause, intervention: Intervention) -> StrategyEntry | None:
        """Look up effectiveness for a specific pair."""
        return self._read(strategy_key(root_cause, intervention))

    def dispose(self) -> None:
        """Clean up keepalive subscription (idempotent)."""
        if self._unsub:
            self._unsub[0]()
            self._unsub.clear()


def strategy_model() -> StrategyModelBundle:
    """Create a strategy model tracking ``root_cause × intervention → success_rate``.

    Pure derived computation — no LLM.
    """
    _map = reactive_map(name="strategy-entries")

    def _compute(deps: list[Any], _actions: Any) -> StrategySnapshot:
        raw = deps[0]
        # ReactiveMapBundle.data is now a MappingProxyType directly
        return dict(raw) if isinstance(raw, MappingProxyType) else {}

    def _strategy_equals(a: StrategySnapshot, b: StrategySnapshot) -> bool:
        if len(a) != len(b):
            return False
        for k, v in a.items():
            bv = b.get(k)
            if bv is None or v.attempts != bv.attempts or v.successes != bv.successes:
                return False
        return True

    snapshot = derived([_map.entries], _compute, name="strategy-model", equals=_strategy_equals)

    # Keep alive so get() works without external subscriber
    unsub = snapshot.subscribe(lambda _msgs: None)

    return StrategyModelBundle(node=snapshot, _map=_map, _unsub=[unsub])


# ---------------------------------------------------------------------------
# Priority scoring
# ---------------------------------------------------------------------------


def priority_score(
    item: NodeImpl[Any],
    strategy: NodeImpl[Any],
    last_interaction_ns: NodeImpl[Any],
    urgency: NodeImpl[Any] | None = None,
    signals: PrioritySignals | None = None,
) -> NodeImpl[Any]:
    """Create a priority scoring derived node for a single triaged item.

    Combines severity weight, attention decay, strategy model effectiveness,
    and an optional external urgency signal.
    """
    sig = signals or PrioritySignals()
    severity_weights = {**DEFAULT_SEVERITY_WEIGHTS, **sig.severity_weights}
    decay_rate = sig.decay_rate
    effectiveness_threshold = sig.effectiveness_threshold
    effectiveness_boost = sig.effectiveness_boost

    deps: list[Any] = [item, strategy, last_interaction_ns]
    if urgency is not None:
        deps.append(urgency)

    def _compute(dep_values: list[Any], _actions: Any) -> float:
        itm = dep_values[0]
        strat = dep_values[1]
        last_ns = dep_values[2]
        urg = dep_values[3] if urgency is not None else 0.0

        sev = getattr(itm, "severity", None) or "medium"
        base_weight = severity_weights.get(sev, 40.0)
        age_seconds = (monotonic_ns() - last_ns) / 1e9
        score = decay(base_weight, age_seconds, decay_rate, 0.0)

        # Strategy model boost
        rc = getattr(itm, "root_cause", None)
        iv = getattr(itm, "intervention", None)
        if rc and iv:
            key = strategy_key(rc, iv)
            entry = strat.get(key) if isinstance(strat, dict) else None
            if entry and entry.success_rate >= effectiveness_threshold:
                score += effectiveness_boost

        # External urgency boost (0–1 scale → 0–20 points)
        score += float(urg) * 20.0

        return score

    return derived(deps, _compute, name="priority-score")
