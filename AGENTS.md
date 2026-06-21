# graphrefly-py -- agent context (Python host package)

GraphReFly Python is the host-language package for Python users. The language-neutral authority lives in `~/src/graphrefly` on branch `clean-slate`; when this repo disagrees with that authority, the authority wins.

## Authority

Read `~/src/graphrefly/CLAUDE.md` first. Key records for this repo:

- `~/src/graphrefly/decisions/decisions.jsonl`
- `~/src/graphrefly/spec/rules.jsonl`
- `~/src/graphrefly/spec/conformance.jsonl`
- `~/src/graphrefly/plan/phases.jsonl`
- `~/src/graphrefly/sessions/active/SESSION-clean-slate-redesign.md`

Sibling implementations:

- TypeScript: `~/src/graphrefly-ts`
- Rust native engine and PyO3 foundation: `~/src/graphrefly-rs`

## CSP-7 Foundation Boundary

- PyPI distribution/project name: `graphrefly`
- import package/module name: `graphrefly`
- product/repo label: `@graphrefly/py`
- private native extension: `graphrefly._native`

Python owns the public facade, typing, decorators/context managers, value registry and lifetime policy, exception taxonomy, async/runtime adapters, ecosystem adapters, and product recipes. Rust owns the synchronous graph engine and native foundation. Do not implement a separate Python wave core.

## Clean-Slate Floor

- No protocol/tier/message/ctx/batch semantic changes from this repo. Route any such need through spec-amend in `~/src/graphrefly`.
- Do not restore the old Python pure-core, `NodeImpl`, `Runner`, `Actor`, GraphSpec, subgraph locks, old Impl/facade/port model, or structural TS parity.
- Cross-runtime parity is behavioral conformance, not matching symbols.
- Async belongs at source/pool/adapter boundaries, not inside the sync wave core.
- Native handles are single-thread host objects in v0.

## Commands

```bash
uv sync --group dev
uv run maturin develop --release
uv run pytest
uv run ruff check .
uv run mypy src
```

Rust foundation check:

```bash
cd ~/src/graphrefly-rs
mise exec -- cargo test -p graphrefly-bindings-py
```
