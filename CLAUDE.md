# graphrefly-py

Python implementation of the GraphReFly reactive graph protocol.

**All operational docs (roadmap, optimizations, test guidance, docs guidance, skills, archive) live in `~/src/graphrefly-ts`.** See that repo's `CLAUDE.md` for the full agent context.

## Commands

uv workspace managed by mise. `mise trust && mise install` to set up uv. `uv sync` to install dependencies.

- Test: `uv run pytest`
- Lint: `uv run ruff check src/ tests/`
- Lint fix: `uv run ruff check --fix src/ tests/`
- Format: `uv run ruff format src/ tests/`
- Type check: `uv run mypy src/`

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

## Key references

| Doc | Location |
|-----|----------|
| Behavior spec | `~/src/graphrefly/GRAPHREFLY-SPEC.md` |
| Composition guide | `~/src/graphrefly/COMPOSITION-GUIDE.md` |
| Roadmap, optimizations, test/docs guidance, skills, archive | `~/src/graphrefly-ts/` (single source of truth) |
| Predecessor (patterns, concurrency) | `~/src/callbag-recharge-py` (reference only, not spec) |

## Design invariants (spec §5.8–5.12)

1. **No polling.** Use reactive timer sources (`from_timer`, `from_cron`).
2. **No imperative triggers.** Use reactive `NodeInput` signals.
3. **No raw async primitives.** Async boundaries belong in sources and runners, not node fns.
4. **Central timer and `message_tier`.** Use `core/clock.py`; never hardcode type checks.
5. **Phase 4+ APIs must be developer-friendly.** No protocol internals in primary surface.
6. **Thread safety.** Per-subgraph `RLock`, per-node `_cache_lock`. Design for GIL and free-threaded Python.
7. **No `async def` in public APIs.** Return `Node[T]`, `Graph`, `None`, or plain synchronous values.
