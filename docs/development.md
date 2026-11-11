# Python development

Use [package metadata](../pyproject.toml) and [docs policy](docs.jsonl) for the
package/tooling contract. The package is pure Python (graphrefly:D800,
graphrefly-py:D6): no Rust toolchain or sibling checkout is needed. uv manages the
environment only; tests and type checks import the package from `src`, and scripts
run with `PYTHONPATH=src` (graphrefly-py:D13).

```sh
uv sync --group dev --group docs
uv run pytest
uv run ruff check .
uv run mypy src
uv run python scripts/check_api_docs.py
uv build
cd website
pnpm docs:gen:check
pnpm build
```

uv manages the environment only (`package = false`); tests import the package from
`src` through pytest's `pythonpath`. Scripts that import it run with
`PYTHONPATH=src`:

```sh
PYTHONPATH=src uv run python scripts/bench_kernel.py
PYTHONPATH=src uv run python scripts/diff_reference.py --count 3000
```

`bench_kernel.py` measures time per node run on the root benchmark scenarios (a
reference, not a gate); `diff_reference.py` compares sink traces with the root
reference kernel on random graphs (needs `node`; reads the root checkout only).

## Package layout

- `graphrefly._kernel` is the private pure-Python kernel for the rules active by
  graphrefly:D798, D799, D801 and D802 (graphrefly-py:D2). It is single-threaded,
  catches `Exception` only (but not `RecursionError` or `MemoryError`, which are
  fatal, graphrefly-py:D10), and has no awaitables. Delivery, activation and
  deactivation run on explicit stacks, not recursion, so a chain's length is not
  bounded by the Python recursion limit (`tests/kernel/test_kernel_depth.py`).
- `graphrefly._checkpoint` is capture and restore over the kernel (R-checkpoint).
- `graphrefly._wire` is the bridge module over the kernel (R-bridge): `bridge` (wire
  edge groups, gate, frame source and sink), `envelope` (numbered envelopes, acks,
  ack-timeout facts) and `protobuf` (the strict canonical codec, no dependency);
  `graphrefly._wire_facade` is its public facade (graphrefly-py:D8). The C-1a tests
  read the golden vectors from the sibling `graphrefly` checkout and skip without it.
- `graphrefly._facade` is the public facade (graphrefly-py:D4); `graphrefly` exports it.
- `tests/kernel/` qualifies the kernel scenarios against `graphrefly._kernel`;
  the other tests cover the facade.

## Python facade boundary

- PyPI distribution/project name: `graphrefly`; import package/module name: `graphrefly`.
- `None` is valid Python DATA. No-DATA is presence, never a `None` sentinel;
  `graphrefly.SENTINEL` is never legal DATA or an ERROR diagnostic.
- `Node.cache()` raises `GraphReflyNoDataError` when no DATA is held;
  `cache(default=...)` is the explicit fallback.
- `subscribe` and `observe` call back once per array, with a tuple of message
  tuples, `(DATA, v)`, `(ERROR, e)` or `(TYPE,)` (graphrefly-py:D12); the first array
  is START and the snapshot. `observe(nodes, callback)` is an ordinary subscriber to
  each node, so it activates them, and hands on `GraphEvent(id, msgs)`
  (R-graph-surface, graphrefly-py:D15).
- The graph layer (R-graph-surface, graphrefly-py:D15): `find(id)` gives the node
  (a state as its `State`) or `None`; `describe()` gives `{id, kind, deps, quiet}`
  per node, deps current; `retain(node)` is a subscriber that ignores what it gets.
  None of `find` and `describe` activates anything.
- `subscribe`, `observe` and `retain` return a stop to call, as TS (`() => void`)
  and Rust (`FnOnce()`) do; `stop()` is idempotent. For `subscribe` and `retain` it
  is an unsubscribe request: called during a wave, the wave's remaining arrays still
  arrive and nothing does after it (R-request-order, C-33). `observe`'s stop takes
  effect at once (R-graph-surface). Python adds a `with` block that stops on exit and
  `.closed`; there is no `unsubscribe` (graphrefly-py:D16, D17).
- Host input lives only on `State` (`set`, `invalidate`, `complete`, `error`);
  `set(callable)` computes from the current value in request order. A plain `Node`
  has no write and no public `down`.
- `Graph.node(deps, callback, ...)` gives the callback a `Ctx`: `ctx.deps[i]` (`id`,
  `waves`, `terminal`, `has`, `value`, `invalidated`), private `state`, `emit`,
  `down(list)` (checked by the kernel), the lifetime `signal` and `rewire`. A
  node's `setup` runs once per lifetime and may return its cleanup. `derived`,
  `effect` and `producer` keep a first-run gate: Python sugar, not protocol.
- A callback `Exception` becomes the node's ERROR as `GraphCallbackError`. A
  subscribe/observe callback `Exception` goes to `Graph(report=...)` as a
  `SubscriberCallbackError` (logged on the `graphrefly` logger without a reporter);
  nothing keeps it (graphrefly-py:D11). A fatal `BaseException` (KeyboardInterrupt, SystemExit, ...), a
  `RecursionError` or a `MemoryError` propagates to the caller and poisons the graph
  facade (graphrefly-py:D2, D10).
- `Graph.close()` / context managers release facade subscriptions (observers and
  retains included) and async work; they send no protocol message.
- Async enters only through runners, sources and adapters; their results are later
  waves on the graph owner thread (graphrefly:D477, D480).

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
- A graph is bound to the thread that created it.
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
