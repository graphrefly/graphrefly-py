# CLAUDE.md

This file provides guidance to coding agents when working with this repository.

## Commands

uv workspace managed by mise. `mise trust && mise install` to set up uv. `uv sync` to
install dependencies.

- Test: `uv run pytest`
- Lint: `uv run ruff check src/ tests/`
- Lint fix: `uv run ruff check --fix src/ tests/`
- Format: `uv run ruff format src/ tests/`
- Type check: `uv run mypy src/`
- Docs site (from `website/`): `pnpm docs:gen` / `pnpm docs:gen:check` — regenerates `src/content/docs/api/` from `extra/tier1.py`, `extra/tier2.py`, and `extra/sources.py` docstrings (see `docs/docs-guidance.md`)

## Package naming

- Distribution name: `graphrefly-py`
- Import path: `graphrefly`

## Layout

- `src/graphrefly/core/` — message protocol, `node` primitive, batch, sugar constructors (Phase 0)
- `src/graphrefly/graph/` — `Graph` container, describe/observe, snapshot (Phase 1+)
- `src/graphrefly/extra/` — operators, sources, data structures, resilience (Phase 2–3)
- `src/graphrefly/patterns/` — domain-layer APIs: orchestration, messaging, memory, AI, CQRS, reactive layout (Phase 4+)
- `src/graphrefly/compat/` — async runners: asyncio, trio (Phase 5+)
- `src/graphrefly/integrations/` — framework integrations: FastAPI (Phase 5+)

## Key docs

- `~/src/graphrefly/GRAPHREFLY-SPEC.md` — protocol and behavioral specification (shared with graphrefly-ts)
- `~/src/graphrefly/COMPOSITION-GUIDE.md` — **composition guide** — 坑, patterns, recipes for Phase 4+ factory authors. Read before building factories that compose primitives.
- `~/src/graphrefly/composition-guide.jsonl` — machine-readable composition entries (appendable)
- `docs/roadmap.md` — phased implementation plan
- `docs/docs-guidance.md` — how to write and maintain documentation here
- `docs/test-guidance.md` — testing conventions and organization
- `archive/docs/SESSION-graphrefly-spec-design.md` — design lineage and rationale vs callbag-recharge

## Design invariants (spec §5.8–5.12)

These are non-negotiable across all implementations. Validate every change against them.

1. **No polling.** State changes propagate reactively via messages. Never poll a node's value on a timer or busy-wait for status. Use reactive timer sources (`from_timer`, `from_cron`) instead.
2. **No imperative triggers.** All coordination uses reactive `NodeInput` signals and message flow through topology. No event emitters, callbacks, or `threading.Timer` + `set()` workarounds. If you need a trigger, it's a reactive source node.
3. **No raw async primitives in the reactive layer.** Do not use bare `asyncio.ensure_future`, `asyncio.create_task`, `threading.Timer`, or raw coroutines to schedule reactive work. Async boundaries belong in sources (`from_awaitable`, `from_async_iter`) and the runner layer (`compat/asyncio_runner`, `compat/trio_runner`), not in node fns or operators.
4. **Central timer and `message_tier` utilities.** Use `clock.py` for all timestamps (see rule below). Use `message_tier` utilities for tier classification — never hardcode type checks for checkpoint or batch gating.
5. **Phase 4+ APIs must be developer-friendly.** Domain-layer APIs (orchestration, messaging, memory, AI, CQRS) use sensible defaults, minimal boilerplate, and clear errors. Protocol internals (`DIRTY`, `RESOLVED`, bitmask) never surface in primary APIs — accessible via `.node()` or `inner` when needed.

## Time utility rule

- Use `src/graphrefly/core/clock.py` utilities for all timestamps.
- Internal/event-order durations must use `monotonic_ns()`.
- Wall-clock attribution payloads must use `wall_clock_ns()`.
- Do not call `time.time_ns()` / `time.monotonic_ns()` directly outside `core/clock.py`.

## Auto-checkpoint trigger rule

- For persistence auto-checkpoint behavior, gate saves by `message_tier >= 2`.
- Do not describe this as DATA/RESOLVED-only; terminal/teardown lifecycle tiers are included.

## Predecessor repo (reference)

For **implementation help**, **test patterns**, **concurrency** (e.g. subgraph locks), and
**porting** lessons from the earlier Python reactive library, agents may read
`~/src/callbag-recharge-py`. That project is **callbag-recharge**, not GraphReFly: the
spec for this repo is **`~/src/graphrefly/GRAPHREFLY-SPEC.md`**. Translate old callbag/STATE/DATA/END
concepts into GraphReFly **Messages** and the single primitive `node`.

## Claude Code skills

Workflow skills live under `.claude/skills/`:

- **`dev-dispatch`** — planned implementation (`/dev-dispatch`) with self-test; suggest `/qa` after
- **`qa`** — adversarial review and final checks (`/qa`)
