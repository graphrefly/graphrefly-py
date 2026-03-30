"""Roadmap §3.2b — composite data patterns (`verifiable`, `distill`)."""

import threading
import time
from collections.abc import Callable

from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import state
from graphrefly.extra.composite import distill, verifiable


def test_verifiable_runs_on_explicit_trigger() -> None:
    src = state(2)
    trigger = state(0)
    bundle = verifiable(
        src,
        lambda value: {"holds": value > 0, "checked": value},
        trigger=trigger,
    )
    assert bundle.verified.get() == {"holds": True, "checked": 2}
    trigger.down([(MessageType.DATA, 1)])
    assert bundle.verified.get() == {"holds": True, "checked": 2}


def test_verifiable_switch_map_cancels_stale_verify() -> None:
    src = state(1)
    trigger = state(0)

    def verify_fn(value: int) -> object:
        def _start(_deps: list[object], actions: object) -> Callable[[], None]:
            delay = 0.05 if value == 1 else 0.005

            def _emit() -> None:
                actions.emit({"value": value})  # type: ignore[attr-defined]
                actions.down([(MessageType.COMPLETE,)])  # type: ignore[attr-defined]

            timer = threading.Timer(delay, _emit)
            timer.daemon = True
            timer.start()
            return timer.cancel

        from graphrefly.core.sugar import producer

        return producer(_start)

    bundle = verifiable(src, verify_fn, trigger=trigger)
    trigger.down([(MessageType.DATA, 1)])
    src.down([(MessageType.DATA, 2)])
    trigger.down([(MessageType.DATA, 2)])
    time.sleep(0.02)
    assert bundle.verified.get() == {"value": 2}
    time.sleep(0.06)
    assert bundle.verified.get() == {"value": 2}


def test_verifiable_accepts_falsy_scalar_trigger() -> None:
    src = state(3)
    bundle = verifiable(src, lambda value: value * 10, trigger=0)
    assert bundle.trigger is not None
    assert bundle.verified.get() == 30


def test_distill_extracts_and_compacts() -> None:
    src = state("alpha")
    bundle = distill(
        src,
        lambda raw, _existing: {
            "upsert": [{"key": raw, "value": {"text": raw, "points": len(raw)}}],
        },
        score=lambda mem, _ctx: float(mem["points"]),
        cost=lambda _mem: 1.0,
        budget=10,
    )
    src.down([(MessageType.DATA, "beta")])
    assert bundle.store.data.get().value["beta"]["text"] == "beta"
    assert bundle.size.get() >= 1
    assert any(entry["key"] == "beta" for entry in bundle.compact.get())


def test_distill_reactive_eviction() -> None:
    src = state("x")
    evict_toggle = state(False)
    bundle = distill(
        src,
        lambda raw, _existing: {"upsert": [{"key": raw, "value": {"text": raw}}]},
        score=lambda _mem, _ctx: 1.0,
        cost=lambda _mem: 1.0,
        budget=10,
        evict=lambda _key, _mem: evict_toggle,
    )
    src.down([(MessageType.DATA, "keep-me")])
    assert "keep-me" in bundle.store.data.get().value
    evict_toggle.down([(MessageType.DATA, True)])
    assert "keep-me" not in bundle.store.data.get().value


def test_distill_consolidates_from_trigger_and_is_atomic() -> None:
    src = state("seed")
    consolidate_trigger = state(False)
    bundle = distill(
        src,
        lambda raw, _existing: {
            "upsert": [{"key": raw, "value": {"text": raw, "points": len(raw)}}],
        },
        score=lambda mem, _ctx: float(mem["points"]),
        cost=lambda _mem: 1.0,
        budget=10,
        consolidate=lambda _entries: {
            "upsert": [{"key": "merged", "value": {"text": "merged", "points": 99}}],
            "remove": ["seed"],
        },
        consolidate_trigger=consolidate_trigger,
    )
    sizes: list[int] = []
    unsub = bundle.size.subscribe(
        lambda msgs: sizes.extend([m[1] for m in msgs if m[0] is MessageType.DATA]),
    )
    consolidate_trigger.down([(MessageType.DATA, True)])
    unsub()

    assert "seed" not in bundle.store.data.get().value
    assert "merged" in bundle.store.data.get().value
    assert all(size >= 1 for size in sizes)


def test_distill_handles_invalid_evict_type_without_side_effects() -> None:
    src = state("x")
    bundle = distill(
        src,
        lambda raw, _existing: {"upsert": [{"key": raw, "value": {"text": raw}}]},
        score=lambda _mem, _ctx: 1.0,
        cost=lambda _mem: 1.0,
        budget=10,
        evict=lambda _key, _mem: "bad",
    )
    src.down([(MessageType.DATA, "y")])
    assert "x" in bundle.store.data.get().value


def test_distill_handles_missing_upsert_without_side_effects() -> None:
    src = state("seed")
    bundle = distill(
        src,
        lambda raw, _existing: (
            {"upsert": [{"key": raw, "value": {"text": raw}}]}
            if raw == "seed"
            else {"remove": ["seed"]}
        ),
        score=lambda _mem, _ctx: 1.0,
        cost=lambda _mem: 1.0,
        budget=10,
    )
    src.down([(MessageType.DATA, "run")])
    assert "seed" in bundle.store.data.get().value


def test_distill_accepts_map_options_dict() -> None:
    src = state("alpha")
    bundle = distill(
        src,
        lambda raw, _existing: {"upsert": [{"key": raw, "value": {"text": raw}}]},
        score=lambda _mem, _ctx: 1.0,
        cost=lambda _mem: 1.0,
        budget=10,
        map_options={"name": "distill-map", "max_size": 2},
    )
    src.down([(MessageType.DATA, "beta")])
    src.down([(MessageType.DATA, "gamma")])
    assert "alpha" not in bundle.store.data.get().value
