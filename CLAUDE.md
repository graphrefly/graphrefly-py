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

## Package naming

- Distribution name: `graphrefly-py`
- Import path: `graphrefly`

## Key docs

- `docs/GRAPHREFLY-SPEC.md` — protocol and behavioral specification (shared with graphrefly-ts)
- `docs/roadmap.md` — phased implementation plan
- `docs/docs-guidance.md` — how to write and maintain documentation here
- `docs/test-guidance.md` — testing conventions and organization
- `archive/docs/SESSION-graphrefly-spec-design.md` — design lineage and rationale vs callbag-recharge

## Predecessor repo (reference)

For **implementation help**, **test patterns**, **concurrency** (e.g. subgraph locks), and
**porting** lessons from the earlier Python reactive library, agents may read
`~/src/callbag-recharge-py`. That project is **callbag-recharge**, not GraphReFly: the
spec for this repo is **`docs/GRAPHREFLY-SPEC.md`**. Translate old callbag/STATE/DATA/END
concepts into GraphReFly **Messages** and the single primitive `node`.

## Claude Code skills

Workflow skills live under `.claude/skills/`:

- **`dev-dispatch`** — planned implementation (`/dev-dispatch`) with self-test; suggest `/qa` after
- **`qa`** — adversarial review and final checks (`/qa`)
