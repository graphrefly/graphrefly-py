# graphrefly-py -- agent context (Python host package)

GraphReFly Python is the host-language package for Python users. The language-neutral authority lives in `~/src/graphrefly`; when this repo disagrees with that authority, the authority wins.

## Authority

Read `~/src/graphrefly/CLAUDE.md` first. Key records for this repo:

- `~/src/graphrefly/decisions/decisions.jsonl`
- `~/src/graphrefly/spec/rules.jsonl`
- `~/src/graphrefly/spec/conformance.jsonl`
- `~/src/graphrefly/plan/phases.jsonl`
- `~/src/graphrefly/sessions/active/SESSION-clean-slate-redesign.md`

Python package-local documentation policy lives in `docs/docs.jsonl`. It is the
source of truth for what this repo owns: Python docstrings, generated Python API
docs, Starlight source pages, examples, PyPI install/release notes, and local docs
checks. Shared graphrefly.dev website architecture, public blog storage/rendering,
shared guide records, protocol authority, and dashboard/control views stay in
`~/src/graphrefly` under D563. Do not copy or migrate shared website/blog/docs
material into this repo, and do not migrate skills.

Sibling implementations:

- TypeScript: `~/src/graphrefly-ts`
- Rust native engine and PyO3 foundation: `~/src/graphrefly-rs`

## CSP-7 Foundation Boundary

- PyPI distribution/project name: `graphrefly`
- import package/module name: `graphrefly`
- product/repo label: `@graphrefly/py`
- private native extension: `graphrefly._native`

Python owns the public facade, typing, decorators/context managers, value registry and lifetime policy, exception taxonomy, async/runtime adapters, ecosystem adapters, and product recipes. Rust owns the synchronous graph engine and native foundation. Do not implement a separate Python wave core.

Current Python value/message boundary:

- `None` is valid Python DATA.
- No-DATA is a private native presence state, not a public `None` sentinel.
- Public `Node.cache()` raises `GraphReflyNoDataError` when no DATA is present; `cache(default=...)` is the explicit fallback path.
- Public observations use `DataMessage`, `ErrorMessage`, and `ControlMessage`.
- Public advanced authoring uses `Graph.node(deps, callback, name=None)`, where
  `callback` receives a Python-owned callback-scoped `Ctx` facade. `Ctx` may expose
  host-natural dep presence/value reads, raw `wave_data`, `terminal(index)`, `emit`,
  per-node `state`, `on_invalidate`, and `on_deactivation`; it is not a raw PyO3
  object and must not expose arbitrary raw protocol message construction/sending.
  `wave_data` is the only raw dep-value input surface (`dep -> waves -> values`):
  no-wave is `[]`, RESOLVED-only is `[[]]`, INVALIDATE projects as exported
  `graphrefly.SENTINEL`, and COMPLETE/ERROR live only in `terminal(index)`. Do not
  add raw `latest`, `prevData`, `latestData`, `depRecords[i].latest`, or equivalent
  parallel value aliases. `graphrefly.SENTINEL` itself is not legal DATA.
- Public graph-owned control convenience is `Graph.pause(node, lock_id)`, `Graph.resume(node, lock_id)`, and `Graph.invalidate(node)`. Do not expose raw protocol ingress or arbitrary public message sending.
- Node callback failures become graph `ERROR` payloads wrapped as `GraphCallbackError`.
- Subscribe/observe callback failures are Python observer-boundary failures captured as `SubscriberCallbackError`.
- Fatal Python `BaseException` process-control failures must propagate to the Python caller, not become graph `ERROR` or `SubscriberCallbackError`. D431 narrows the native batch edge: batch-body rollback remains normal, but a fatal first observed after native commit has begun is a host-boundary abort without a full transactional rollback claim. D436 requires the public Python facade to auto-close/poison after such fatal propagation.
- `Graph.close()` / context managers are Python host lifetime scopes only: release facade-created subscriptions/observers and reject later facade use without protocol `TEARDOWN`/`COMPLETE`.

## Clean-Slate Floor

- No protocol/tier/message/ctx/batch semantic changes from this repo. Route any such need through spec-amend in `~/src/graphrefly`.
- Do not restore retired pre-clean-slate Python core surfaces, subgraph locks,
  the old port model, or structural cross-language parity.
- Cross-runtime parity is behavioral conformance, not matching symbols.
- Async belongs at source/pool/adapter boundaries, not inside the sync wave core.
- Native handles are single-thread host objects in this foundation slice.
- Generated documentation output must be regenerated through the Starlight API generator,
  not edited by hand.

## Commands

```bash
uv sync --group dev --group docs
uv run maturin develop --release
uv run pytest
uv run ruff check .
uv run mypy src
cd website && pnpm build
```

Rust foundation check:

```bash
cd ~/src/graphrefly-rs
mise exec -- cargo test -p graphrefly-bindings-py
```
