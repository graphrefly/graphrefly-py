"""Actor context, capability guards, and policy builder (roadmap Phase 1.5)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
    """Build a guard function from declarative allow/deny rules.

    Precedence: for a given ``(actor, action)``, any matching ``deny`` wins
    (returns ``False``). Otherwise, any matching ``allow`` returns ``True``.
    If no rule matches, the result is ``False``.

    Args:
        build: Callable invoked with ``(allow, deny)`` accumulators. Each accumulator
            accepts an ``action`` (str or list of str, supports ``"*"`` wildcard) and
            an optional ``where`` predicate ``(actor) -> bool``.

    Returns:
        A :data:`GuardFn` ``(actor, action) -> bool``.

    Example:
        ```python
        from graphrefly import state, policy
        g = policy(lambda allow, deny: [
            allow("write", where=lambda a: a.get("role") == "admin"),
            deny("write", where=lambda a: a.get("type") == "llm"),
        ])
        x = state(0, guard=g)
        ```
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


def policy_from_rules(rules: list[dict[str, Any]]) -> GuardFn:
    """Rebuild a declarative guard from persisted rule data.

    Rule schema:
    - ``effect``: ``"allow"`` or ``"deny"``
    - ``action``: str or list[str]
    - ``actorType``: optional str or list[str]
    - ``actorId``: optional str or list[str]
    - ``claims``: optional dict[str, Any] exact-match constraints
    """

    def build(
        allow: Callable[..., None],
        deny: Callable[..., None],
    ) -> None:
        for rule in rules:
            effect = str(rule.get("effect", "allow")).lower()
            if effect not in ("allow", "deny"):
                raise ValueError(f"policy_from_rules unknown effect {effect!r}")
            action_value = rule.get("action", "write")
            actor_type_raw = rule.get("actorType")
            actor_id_raw = rule.get("actorId")
            claims = rule.get("claims")
            actor_types = (
                None
                if actor_type_raw is None
                else set(actor_type_raw if isinstance(actor_type_raw, list) else [actor_type_raw])
            )
            actor_ids = (
                None
                if actor_id_raw is None
                else set(actor_id_raw if isinstance(actor_id_raw, list) else [actor_id_raw])
            )
            claim_items = list(claims.items()) if isinstance(claims, dict) else []

            def where(
                actor: Actor,
                _types: set[Any] | None = actor_types,
                _ids: set[Any] | None = actor_ids,
                _claims: list[tuple[Any, Any]] = claim_items,
            ) -> bool:
                if _types is not None and actor.get("type") not in _types:
                    return False
                if _ids is not None and actor.get("id", "") not in _ids:
                    return False
                return all(actor.get(str(k)) == v for k, v in _claims)

            if effect == "deny":
                deny(action_value, where=where)
            else:
                allow(action_value, where=where)

    return policy(build)


def compose_guards(*guards: GuardFn | None) -> GuardFn:
    """Compose multiple guard functions with AND logic; ``None`` entries are skipped.

    Args:
        *guards: Any number of :data:`GuardFn` callables or ``None``. ``None`` values
            are silently filtered out.

    Returns:
        A :data:`GuardFn` that returns ``True`` only when every non-None guard
        approves the ``(actor, action)`` pair.

    Example:
        ```python
        from graphrefly.core.guard import compose_guards, policy
        g1 = policy(lambda allow, _: allow("write"))
        g2 = policy(lambda allow, _: allow("observe"))
        combined = compose_guards(g1, g2)
        ```
    """
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


@dataclass(frozen=True, slots=True)
class MutationRecord:
    """Snapshot for :attr:`~graphrefly.core.node.NodeImpl.last_mutation`."""

    actor: dict[str, Any]
    timestamp_ns: int


def record_mutation(actor: Mapping[str, Any]) -> MutationRecord:
    """Snapshot for :attr:`~graphrefly.core.node.NodeImpl.last_mutation`."""
    from graphrefly.core.clock import wall_clock_ns

    return MutationRecord(actor=dict(normalize_actor(actor)), timestamp_ns=wall_clock_ns())


__all__ = [
    "Actor",
    "GuardAction",
    "GuardDenied",
    "GuardFn",
    "MutationRecord",
    "access_hint_for_guard",
    "compose_guards",
    "normalize_actor",
    "policy",
    "policy_from_rules",
    "record_mutation",
    "system_actor",
]
