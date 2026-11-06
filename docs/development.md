# Python development

Use [package metadata](../pyproject.toml) and [docs policy](docs.jsonl) for the
package/tooling contract. The native engine is in the sibling Rust repository.

```sh
uv sync --group dev --group docs
uv run maturin develop --release
uv run pytest
uv run ruff check .
uv run mypy src
uv run python scripts/check_api_docs.py
cd website
pnpm docs:gen:check
pnpm build
```

Maturin needs the Rust toolchain from the sibling checkout. In an agent shell,
run it through that checkout's mise environment while keeping uv's working
project here. Follow [Rust development](../../graphrefly-rs/docs/development.md)
for toolchain and long-command observation. The native binding gate is
`mise exec -- cargo test -p graphrefly-bindings-py` from the Rust checkout when
that foundation is affected.

## Existing Python facade boundary

The following moved from AGENTS under approved B142. It retains existing package
guidance; no D796 draft is activated or implemented by this document.


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


## Existing contribution and migration constraints


When changing authority records or resolving an execution, spend, retry, or stalled-work
authorization boundary, load and follow the personal
`$project-governance` skill at `~/.codex/skills/project-governance/SKILL.md`. It governs
cross-project record and permission classification; Python-local and root GraphReFly authorities
remain canonical for their own concerns. The concrete GraphReFly family mapping is proposed as
`graphrefly:B137`; do not invent that schema or migrate history before its separate approval, and do
not mint a D# merely for an attempt, rerun, receipt, incident, provider/model change, or spend grant.

- No protocol/tier/message/ctx/batch semantic changes from this repo. Route any such need through spec-amend in `~/src/graphrefly`.
- Do not restore retired pre-clean-slate Python core surfaces, subgraph locks,
  the old port model, or structural cross-language parity.
- Cross-runtime parity is behavioral conformance, not matching symbols.
- Async belongs at source/pool/adapter boundaries, not inside the sync wave core.
- Native handles are single-thread host objects in this foundation slice.
- Generated documentation output must be regenerated through the Starlight API generator,
  not edited by hand.
- Decision admission is owner-first: Python-only facade, lifetime, packaging or ecosystem locks
  belong in the local ledger; protocol, cross-runtime and cross-project locks remain upstream.
  New cross-repo refs are origin-qualified and decision bodies are never copied.


## Documentation and review

[Docs policy](docs.jsonl) owns local docstrings, generated Python API, Starlight
source pages, examples and release material. Shared website/blog/protocol content
remains [root-owned](../../graphrefly/guide/docs.jsonl); do not migrate skills.
Generated API output must be regenerated from public docstrings through the
Starlight/griffe pipeline, not edited by hand.

Follow [the authority contract](../../graphrefly/authority/README.md) for admission;
root-origin history is relocation-only. Local/shared build, QA and human review
route through [AGENTS](../AGENTS.md). See the [category map](README.md) for current
work, drafts and executed evidence. A passing check is not acceptance or dispatch.
