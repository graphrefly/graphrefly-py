"""Actor context, capability guards, and policy builder (roadmap Phase 1.5)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, TypedDict

type GuardAction = str
"""Known actions: ``"write"``, ``"signal"``, ``"observe"``.

Open type (plain ``str``) so callers can define domain-specific actions
(e.g. ``"admin"``, ``"delete"``) or use the wildcard ``"*"`` in
:func:`policy` rules.  Aligned with the TS ``(string & {})`` pattern.
"""

#: ``(actor, action) -> bool`` — when ``False``, APIs raise :exc:`GuardDenied`.
GuardFn = Callable[["Actor", GuardAction], bool]


class Actor(TypedDict, total=False):
    """Who is acting. Extra string keys are allowed at runtime (ABAC claims)."""

    type: str
    id: str


def system_actor() -> Actor:
    """Default actor for mutations that do not pass an explicit context."""
    return {"type": "system", "id": ""}


def normalize_actor(actor: Mapping[str, Any] | Actor | None) -> dict[str, Any]:
    """Merge with :func:`system_actor` defaults (missing ``type`` / ``id`` filled)."""
    base: dict[str, Any] = {"type": "system", "id": ""}
    if actor is not None:
        base.update(dict(actor))
    if not base.get("type"):
        base["type"] = "system"
    if "id" not in base:
        base["id"] = ""
    return base


class GuardDenied(Exception):
    """Raised when a node's guard rejects an action."""

    __slots__ = ("action", "actor", "node")

    def __init__(
        self,
        actor: Mapping[str, Any],
        node: str,
        action: GuardAction,
    ) -> None:
        self.actor = dict(actor)
        self.node = node
        self.action = action
        super().__init__(f"guard denied {action!r} on node {node!r} for actor {self.actor!r}")


def _normalize_actions(
    action: GuardAction | list[GuardAction] | tuple[GuardAction, ...],
) -> frozenset[str]:
    """Accept a single action string or a sequence and return a frozen set."""
    if isinstance(action, str):
        return frozenset((action,))
    return frozenset(action)


def _matches_actions(actions: frozenset[str], guard_action: GuardAction) -> bool:
    """Check membership with ``"*"`` wildcard support (aligned with TS)."""
    return guard_action in actions or "*" in actions


def _policy_rule_result(
    *,
    actions: frozenset[str],
    where: Callable[[Actor], bool] | None,
    actor: Mapping[str, Any],
    guard_action: GuardAction,
) -> bool:
    return _matches_actions(actions, guard_action) and (where is None or where(actor))  # type: ignore[arg-type]


def policy(
    build: Callable[
        [
            Callable[..., None],
            Callable[..., None],
        ],
        Any,
    ],
) -> GuardFn:
    """Build a guard from declarative allow/deny rules.

    **Precedence (C):** For a fixed ``(actor, action)``, if **any** matching **deny**
    rule applies, the result is ``False``. Otherwise, if **any** matching **allow**
    applies, the result is ``True``. If no rule matches, the result is ``False``.

    Usage::

        g = policy(lambda allow, deny: [
            allow("write", where=lambda a: a.get("role") == "admin"),
            deny("write", where=lambda a: a.get("type") == "llm"),
        ])
    """
    Rule = tuple[Literal["allow", "deny"], frozenset[str], Callable[[Actor], bool] | None]
    rules: list[Rule] = []

    def allow(
        action: GuardAction | list[GuardAction] | tuple[GuardAction, ...],
        *,
        where: Callable[[Actor], bool] | None = None,
    ) -> None:
        rules.append(("allow", _normalize_actions(action), where))

    def deny(
        action: GuardAction | list[GuardAction] | tuple[GuardAction, ...],
        *,
        where: Callable[[Actor], bool] | None = None,
    ) -> None:
        rules.append(("deny", _normalize_actions(action), where))

    build(allow, deny)

    def guard(actor: Actor, guard_action: GuardAction) -> bool:
        a = normalize_actor(actor)
        denied = False
        allowed = False
        for kind, acts, where in rules:
            matched = _policy_rule_result(
                actions=acts,
                where=where,
                actor=a,
                guard_action=guard_action,
            )
            if not matched:
                continue
            if kind == "deny":
                denied = True
            else:
                allowed = True
        if denied:
            return False
        return allowed

    return guard


def compose_guards(*guards: GuardFn | None) -> GuardFn:
    """AND-composition; ``None`` entries are skipped."""
    gs = [g for g in guards if g is not None]

    def composed(actor: Actor, action: GuardAction) -> bool:
        return all(g(actor, action) for g in gs)

    return composed


_STANDARD_WRITE_TYPES = ("human", "llm", "wallet", "system")


def access_hint_for_guard(guard: GuardFn) -> str:
    """Best-effort ``meta.access`` string when a guard is present (roadmap 1.5)."""
    allowed = [t for t in _STANDARD_WRITE_TYPES if guard({"type": t, "id": ""}, "write")]
    if not allowed:
        return "restricted"
    if "human" in allowed and "llm" in allowed and set(allowed) <= {"human", "llm", "system"}:
        return "both"
    if len(allowed) == 1:
        return allowed[0]
    return "+".join(allowed)


def record_mutation(actor: Mapping[str, Any]) -> dict[str, Any]:
    """Snapshot for :attr:`~graphrefly.core.node.NodeImpl.last_mutation`."""
    from graphrefly.core.clock import wall_clock_ns

    return {"actor": dict(normalize_actor(actor)), "timestamp_ns": wall_clock_ns()}


__all__ = [
    "Actor",
    "GuardAction",
    "GuardDenied",
    "GuardFn",
    "access_hint_for_guard",
    "compose_guards",
    "normalize_actor",
    "policy",
    "record_mutation",
    "system_actor",
]
