# Roadmap — Active Items

> **Completed phases and items have been archived to `archive/roadmap/*.jsonl`.** See `docs/docs-guidance.md` § "Roadmap archive" for the archive structure and workflow.
>
> **Spec:** `~/src/graphrefly/GRAPHREFLY-SPEC.md` (canonical; not vendored in this repo)
>
> **Contributing docs:** [docs-guidance.md](docs-guidance.md), [test-guidance.md](test-guidance.md)
>
> **Predecessor:** callbag-recharge-py (Phase 0-1 complete: 6 core primitives, 18 operators,
> utils/resilience, 100+ tests, per-subgraph concurrency). Key patterns and lessons carried
> forward — see `archive/docs/DESIGN-ARCHIVE-INDEX.md` for lineage.

---

## Harness Engineering Sprint — Priority Build Order

> **Context:** Mirrors the TS harness engineering sprint (`graphrefly-ts/docs/roadmap.md`). Python tracks TS for core parity on the audit/accountability layer (§9.2). Eval harness is TS-primary (corpus, rubrics, runner). MCP server and framework infiltration packages are TS-only. Python focus: §9.2 parity + backpressure.
>
> **Design reference:** `~/src/graphrefly-ts/archive/docs/SESSION-harness-engineering-strategy.md`

### Wave 1 (Python): Eval parity prep (Weeks 1-3)

No new Python deliverables — Wave 1 is TS-primary (eval runner, CI, blog). Python work during this window:

#### 9.0 — Architecture debt (8.2 rearchitecture)

> DONE — archived to `archive/roadmap/phase-9-harness-sprint.jsonl` (id: `9.0-architecture-debt`).

### Wave 2 (Python): Audit & accountability parity (Weeks 4-9)

#### 9.2 — Audit & accountability (8.4 → 9.2) — TS parity

- [ ] `explain_path(graph, from_node, to_node)` — walk backward through graph derivation chain. Human + LLM readable causal chain. (8.4 → 9.2)
- [ ] `audit_trail(graph, opts)` → Graph — wraps graph with `reactive_log` recording every mutation, actor, timestamp, causal chain. (8.4 → 9.2)
- [ ] `policy_enforcer(graph, policies)` — reactive constraint enforcement. Policies are nodes. Violations → alert subgraph. (8.4 → 9.2)
- [ ] `compliance_snapshot(graph)` — point-in-time export for regulatory archival. (8.4 → 9.2)

#### 9.2b — Backpressure protocol (8.5 → 9.2b)

TS has this shipped; Python needs parity for production reduction pipelines.

- [ ] Backpressure protocol — formalize PAUSE/RESUME for throughput control across graph boundaries (8.5 → 9.2b)

### Wave 3 (Python): Polish & publish (Weeks 10-15)

- [ ] `llms.txt` for AI agent discovery (7 → 9.3)
- [ ] PyPI publish: `graphrefly-py` (7 → 9.3)
- [ ] Docs site at `py.graphrefly.dev` (7 → 9.3)

### Inspection Tool Consolidation (cross-cutting, TS + PY)

PY consolidation — reduce inspection surface from 14+ tools to 9 with clear, non-overlapping responsibilities.

**Design reference:** `archive/docs/SESSION-inspection-consolidation.md` (in graphrefly-ts)

#### PY consolidation (DONE 2026-04-08)

- [x] Merge `spy()` into `observe(format=)` — `format="pretty"|"json"` on `observe()` replaces `spy()`
- [x] Add `trace()` (write + read), absorb `annotate()` + `trace_log()`
- [x] Unexport `describe_node`, `meta_snapshot` from public API (internal to `core.meta`)
- [x] `harness_trace()` pipeline stage tracer (`patterns/harness/trace.py`)
- [x] Runner `__repr__` with `_scheduled`/`_completed` counters on `AsyncioRunner` and `TrioRunner`

#### PY parity items already done

- [x] `Graph.diff()` — already implemented

### Deferred (post-Wave 3)

- §7.2 Showcase demos (Pyodide/WASM lab) — after TS demos prove the pattern
- §7.2b Universal reduction demos — after Demo 0 + Demo 6
- §7.3 Scenario tests — after demos
- §7.4 Inspection stress tests
- §7.5 Foreseen building blocks
- §8.5 `peer_graph`, `sharded_graph`, adaptive sampling — distributed scale
- §6.2 V2 schema, §6.3 V3 caps+refs — versioning depth
- Free-threaded Python 3.14 benchmark suite — perf exploration

---

## Open items from completed phases

Items that were not done when their parent phase shipped. Tracked here for visibility.

### Phase 0.6 — Sugar constructors (omitted by design)

- [ ] `subscribe(dep, callback)` — omitted (match graphrefly-ts): use `node([dep], fn)` or `effect([dep], fn)`
- [ ] `operator(deps, fn, opts?)` — omitted; use `derived`

### Phase 6.1 — V1 content addressing

- [ ] Lazy CID computation — computed on first access after value change, not on every DATA

### Phase 6.2 — V2: + schema (type validation)

- [ ] V2: + schema (type validation at node boundaries)

### Phase 6.3 — V3: + caps + refs

- [ ] V3: + caps + refs — serialization/transport format for runtime enforcement (Phase 1.5)

### Phase 7 — Polish & Launch

- [ ] `llms.txt` for AI agent discovery
- [ ] PyPI publish: `graphrefly-py`
- [ ] Docs site
- [ ] Benchmarks vs RxPY, manual state, asyncio patterns
- [ ] Free-threaded Python 3.14 benchmark suite
- [ ] Community launch

### Phase 7.2 — Showcase demos (Pyodide/WASM lab)

- [ ] **Demo 1: Order Processing Pipeline** — 4.1 + 4.2 + 4.5 + 1.5 (Pyodide, headless + text output)
- [ ] **Demo 2: Multi-Agent Task Board** — 4.1 + 4.3 + 4.4 + 3.2b + 1.5 (Pyodide, mock LLM)
- [ ] **Demo 3: Real-Time Monitoring Dashboard** — 4.1 + 4.2 + 4.3 + 3.1 + 3.2 (Pyodide)
- [ ] **Demo 4: AI Documentation Assistant** — 4.3 + 4.4 + 3.2b + 3.2 + 3.1 (Pyodide, mock LLM)

### Phase 7.2b — Universal reduction demos

- [ ] **Demo 5: Observability Pipeline** — 5.3b + 8.1 + 8.4 + 3.2b
- [ ] **Demo 6: AI Agent Observatory** — 4.4 + 8.1 + 8.4
- [ ] **Demo 7: Log Reduction Pipeline** — 5.3b + 8.1 + 8.2

### Phase 7.3 — Scenario tests

- [ ] `tests/scenarios/test_order_pipeline.py`
- [ ] `tests/scenarios/test_agent_task_board.py`
- [ ] `tests/scenarios/test_monitoring_dashboard.py`
- [ ] `tests/scenarios/test_docs_assistant.py`

### Phase 7.4 — Inspection stress & adversarial tests

- [ ] `describe()` consistency during batch drain
- [ ] `observe()` structured/causal/timeline correctness under concurrent updates
- [ ] `Graph.diff()` performance on 500-node graphs
- [ ] `to_mermaid()` output validity
- [ ] `trace_log()` ring buffer wrap correctness
- [ ] Cross-factory composition: mounted subgraphs don't interfere
- [ ] Guard bypass attempts (`.down()` without actor)
- [ ] `snapshot()` during batch drain (consistent, never partial)
- [ ] `subscription()` added mid-drain (correct offset)
- [ ] `collection()` eviction during derived read (no stale refs)
- [ ] Thread-safety: concurrent factory composition under per-subgraph locks

### Phase 7.5 — Foreseen building blocks

- [ ] **Reactive cursor** (shared by `subscription()` + `job_queue()`) — cursor advancing through `reactive_log`
- [ ] **Streaming node convention** — partial value emission for `chat_stream()`/`from_llm()` token-by-token output
- [ ] **Factory composition helper** — shared pattern for 4.x graph factory boilerplate
- [ ] **Guard-aware describe for UI** — `describe(show_denied=True)` variant
- [ ] **Mock LLM fixture system** — `mock_llm(responses)` adapter for `from_llm()`
- [ ] **Time simulation** — `monotonic_ns()` test-mode override

### Phase 8.4 — Audit & accountability

- [ ] `audit_trail(graph, opts)` → Graph
- [ ] `explain_path(graph, from_node, to_node)` — causal chain
- [ ] `policy_enforcer(graph, policies)` — reactive constraint enforcement
- [ ] `compliance_snapshot(graph)` — regulatory archival

### Phase 8.5 — Performance & scale

- [ ] Backpressure protocol — formalize PAUSE/RESUME for throughput control
- [ ] `peer_graph(transport, opts)` — federate graphs across processes/services
- [ ] Benchmark suite: 10K nodes, 100K msgs/sec
- [ ] `sharded_graph(shard_fn, opts)` — partition across `multiprocessing` workers
- [ ] Adaptive sampling
- [ ] Free-threaded Python 3.14 benchmark for parallel reduction branches

---

## Effort Key

| Size | Meaning |
|------|---------|
| **S** | Half day or less |
| **M** | 1-2 days |
| **L** | 3-4 days |
| **XL** | 5+ days |
