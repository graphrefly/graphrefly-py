# graphrefly-py — agent context

**GraphReFly** — reactive graph protocol for human + LLM co-operation. This package is the Python implementation (`graphrefly-py` on PyPI, `import graphrefly`).

## Canonical references (read these)

| Doc | Role |
|-----|------|
| `~/src/graphrefly/GRAPHREFLY-SPEC.md` | **Behavior spec** — messages, `node`, `Graph`, invariants |
| `docs/roadmap.md` | Phased implementation checklist |
| `docs/optimizations.md` | Cross-language notes, open design decisions |
| `docs/test-guidance.md` | How to write and organize tests |

## Sibling repos

| Repo | Path | Role |
|------|------|------|
| `graphrefly-ts` | `~/src/graphrefly-ts` | TypeScript implementation (must stay in parity) |
| `graphrefly` (spec) | `~/src/graphrefly` | Contains `GRAPHREFLY-SPEC.md` (behavior authority) |
| `callbag-recharge` | `~/src/callbag-recharge` | Predecessor (patterns/tests, NOT spec authority) |

## Layout

- `src/graphrefly/core/` — message protocol, `node` primitive, batch, sugar constructors (Phase 0)
- `src/graphrefly/graph/` — `Graph` container, describe/observe, snapshot (Phase 1+)
- `src/graphrefly/extra/` — operators and sources (Phase 2+)
- `src/graphrefly/patterns/` — domain layer factories (Phase 4+)

## Commands

```bash
uv run pytest              # tests
uv run ruff check src/     # lint
uv run ruff check --fix src/ tests/  # lint fix
uv run mypy src/           # type check
```

## Key invariants

- Messages are always `list[tuple[Type, Any] | tuple[Type]]` — no single-message shorthand.
- DIRTY before DATA/RESOLVED in two-phase push; batch defers DATA, not DIRTY.
- Unknown message types forward — do not swallow.
- No `async def` / `Awaitable` in public API return types — use `Node[T]` or `None`.
- Use `src/graphrefly/core/clock.py` for timestamps (`monotonic_ns()` for event order, `wall_clock_ns()` for attribution).
- `~/src/graphrefly/GRAPHREFLY-SPEC.md` is the behavior authority, not the Python or TS code.
- Per-subgraph `RLock` concurrency — all public APIs must be thread-safe.
- Must pass under both GIL-enabled and free-threaded Python 3.14.

## Agent skills

Project-local skills live under `.gemini/skills/`:

- **dev-dispatch** — implement feature/fix with planning, spec alignment, and self-test. Always halts for approval before implementing.
- **parity** — cross-language parity check against `graphrefly-ts` (read-only until approved)
