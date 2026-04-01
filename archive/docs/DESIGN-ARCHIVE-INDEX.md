# Design Decision Archive

This directory preserves detailed design discussions from key sessions. These are not casual notes — they capture the reasoning chains, rejected alternatives, and insights that shaped the architecture.

## Predecessor: callbag-recharge-py

GraphReFly-py is the successor to [callbag-recharge-py](https://github.com/nicepkg/callbag-recharge-py) (Phase 0-1 complete: 6 core primitives, 18 operators, utils/resilience, 100+ tests) and draws from [callbag-recharge](https://github.com/nicepkg/callbag-recharge) (TS, 170+ modules). The full design history is preserved in those repos under `src/archive/docs/DESIGN-ARCHIVE-INDEX.md`.

Key sessions from the predecessor that directly informed GraphReFly:

| Session | Repo | Date | What it established |
|---------|------|------|-------------------|
| Type 3 Control Channel (8452282f) | TS | Mar 14 | Separating control from data — evolved into unified message protocol |
| Push-Phase Memoization (ce974b95) | TS | Mar 14 | RESOLVED signal for transitive skip — carried forward |
| Explicit Deps (05b247c1) | TS | Mar 14 | No implicit tracking — carried forward |
| Python Port Strategy | Py | Mar 25 | Protocol classes, unlimited bitmask, per-subgraph locks, core sync |
| Lazy Tier 2 (lazy-tier2-option-d3) | TS | Mar 18 | get() doesn't guarantee freshness — evolved into status-based trust |
| Vision: LLM Actuator (vision-llm-actuator-jarvis) | Both | Mar 26-27 | Three-layer vision, Graph as universal output — directly triggered GraphReFly |

### Python-specific lessons carried forward

| Lesson | Source | Impact on GraphReFly |
|--------|--------|---------------------|
| Typed Protocol classes > integer tags | Python port | Spec uses typed interfaces, not callbag function signatures |
| Unlimited-precision bitmask | Python `int` | Implementation simplicity, no fallback code |
| Per-subgraph RLock concurrency | Python threading | Spec defines logical concurrency, Python adds real parallelism |
| Core 100% synchronous | Python port | No asyncio for basic state, async at boundaries via Runner |
| Superset deps at construction | dynamic_derived | Merged dynamicDerived into derived → then into single `node` primitive |
| Context managers | Python idiom | `with batch():` and context manager subscribe stay |
| Free-threaded Python 3.14 | Architecture decision | Correct under both GIL modes from day one |

---

## GraphReFly Sessions

### Session graphrefly-spec-design (March 27) — Unified Spec: Protocol, Single Primitive, Graph Container
**Topic:** Designing the GraphReFly unified cross-repo spec through a 7-step process. Radical simplification from callbag-recharge's 6 primitives + 4 callbag types to 1 primitive (`node`) + unified message format.

**Process:** Lessons learned → demands/gaps → functionalities → common patterns → primitives → nice-to-haves → scenario validation.

**Key decisions:**
- **One primitive: `node(deps?, fn?, opts?)`** — behavior from configuration, sugar constructors for readability
- **Unified message format:** always `[[Type, Data?], ...]`, 9 types, no channel separation
- **Unified node interface:** `.get()` (cached, never errors), `.status`, `.down()`, `.up()`, `.unsubscribe()`, `.meta`
- **Meta as companion stores** — each key subscribable, replaces all `with_*()` wrappers
- **No separate Knob/Gauge/Inspector** — `describe()` + `observe()` on Graph
- **Pure wire edges** — no transforms, everything is a node
- **Colon namespace** — `"system:payment:validate"`

**Validated scenarios:** LLM cost control, security policy, human-in-the-loop, Excel calculations, multi-agent routing, LLM graph building, git versioning.

**Outcome:** `~/src/graphrefly/GRAPHREFLY-SPEC.md` (v0.1.0), `docs/roadmap.md`, new repo decision.

### Session access-control-actor-guard (March 28) — Built-in ABAC: Actor, Guard, Policy Builder
**Topic:** Designing built-in access control for GraphReFly that replaces external authz libraries (e.g. CASL). The graph is the single enforcement point — every mutation flows through `down()`/`set()`/`signal()`, so one guard per node is complete coverage.

**Key decisions:**
- **Three primitives:** Actor context (who), capability guard (may they), scoped introspection (what can they see)
- **`policy()` declarative builder** — CASL-style `allow()`/`deny()` DX, zero dependencies
- **Attribution pulled to Phase 1.5** — `node.last_mutation` records `{ actor, timestamp }` on every mutation
- **CASL rejected** as dependency — its subject model, sift.js query engine, and pack/unpack serialization are unnecessary when the graph is the only enforcement point
- **Web3 identity maps cleanly** — wallet signatures, x402 proofs, ERC-8004 agent IDs all produce actors; the guard is identity-mechanism-agnostic

**Roadmap impact:** New Phase 1.5 (Actor & Guard), expanded Phase 1.6 (tests), Phase 5.4 accepts `actor?`, Phase 6 simplified.

**Files:** `archive/docs/SESSION-access-control-actor-guard.md`

### Session cross-repo-implementation-audit (March 29) — Python companion to TS audit program
**Topic:** Same **`~/src/graphrefly-ts/docs/audit-plan.md`** program; batch write-ups live in **`~/src/graphrefly-ts/docs/batch-review/`**. This repo’s session file summarizes **Python-specific** follow-ups (batch drain vs TS, xfailed operator tests, SQLite checkpoint concurrency, type-alias hygiene).

**Canonical narrative:** `~/src/graphrefly-ts/archive/docs/SESSION-cross-repo-implementation-audit.md`

**Files:** `archive/docs/SESSION-cross-repo-implementation-audit.md`

### Session tier2-parity-nonlocal-forward-inner (March 30) — Tier 2 Operator Parity: forwardInner, nonlocal, sample Architecture
**Topic:** Cross-repo parity fixes for three divergences in Python's Tier 2 operators. All code changes are in this repo; TS was already correct.

**Key decisions:**
- **`_forward_inner` emitted flag** — prevents double-DATA when inner emits during subscribe (Python subscribe doesn't auto-emit initial DATA like TS)
- **`nonlocal` replaces `[value]` list-boxing** — idiomatic Python 3 across all 18 operators
- **`sample` rewritten to dep+onMessage** — eliminates mirror node; uses `node([src, notifier], compute, on_message=...)` matching TS
- **Tier 2 regression testing is the top testing priority** — composite message ordering, void sources through `*_map`, PAUSE/RESUME/INVALIDATE through inners, timer callbacks during batch drain

**Canonical log:** `~/src/graphrefly-ts/archive/docs/SESSION-tier2-parity-nonlocal-forward-inner.md`

**Files:** `archive/docs/SESSION-tier2-parity-nonlocal-forward-inner.md`

### Session demo-test-strategy (March 30) — Demo & Test Strategy: 4 Demos, Inspection-as-Test-Harness, Foreseen Building Blocks
**Topic:** Demo and test strategy for Phase 4 domain layers. Companion to canonical design in `~/src/graphrefly-ts/docs/demo-and-test-strategy.md`. Python-specific: demos run in Pyodide/WASM lab, thread-safety is a first-class test axis (per-subgraph `RLock`, free-threaded Python 3.14), mock LLM must be thread-safe.

**Key decisions:**
- Four demos mirroring TS topology using Python APIs (text/table output in Pyodide REPL)
- Scenario tests (`tests/scenarios/test_*.py`) mirror TS demo AC lists, adapted for `snake_case` and context managers
- Adversarial tests include thread-safety dimension (concurrent factory composition, snapshot during batch drain with contention)
- Foreseen building blocks include thread-safe mock LLM and Pyodide-safe fallbacks

**Canonical document:** `~/src/graphrefly-ts/docs/demo-and-test-strategy.md`

**Roadmap impact:** New Phase 7.1–7.4.

**Files:** `archive/docs/SESSION-demo-test-strategy.md`

### Session universal-reduction-layer (March 31) — Universal Reduction Layer: Massive Info → Actionable Items via LLM-Composable Reactive Graphs
**Topic:** Generalizing GraphReFly from domain-specific tools (issue tracker, observability) to a universal reactive reduction layer for any "massive info → actionable items" pattern. Python companion to the canonical session in `~/src/graphrefly-ts/archive/docs/SESSION-universal-reduction-layer.md`.

**Python-specific considerations:**
- Adapter ecosystem advantages: `confluent-kafka`, `clickhouse-connect`, `boto3`, `redis-py` are all mature Python libraries
- Concurrency: per-subgraph `RLock` critical for high-throughput reduction branches; free-threaded Python 3.14 enables true parallelism
- OTel bridge: `from_otel()` should accept `opentelemetry-sdk` SpanProcessor/MetricReader/LogEmitterProvider interfaces
- `multiprocessing`-based `sharded_graph` for scaling beyond single process

**Roadmap impact:** New Phase 5.3b (ingest adapters), 5.3c (storage/sink adapters), Phase 8 (Universal Reduction Layer: 8.1–8.5), Phase 7.1b (3 universal reduction demos).

**Files:** `archive/docs/SESSION-universal-reduction-layer.md`

### Session serialization-memory-footprint (March 31) — Adoption Blockers: Memory Footprint, DAG-CBOR Codec, Tiered Representation, NodeV0 Promotion
**Topic:** Python companion to the canonical session in `~/src/graphrefly-ts/archive/docs/SESSION-serialization-memory-footprint.md`. Covers DAG-CBOR as default codec, tiered representation, five memory tuning strategies, delta checkpoints, and NodeV0 promotion.

**Python-specific considerations:**
- DAG-CBOR via `cbor2` (C-accelerated) with custom CID tag handler, or `dag-cbor` for strict IPLD interop; `zstandard` for compression
- Python per-node overhead is ~1200-1500 bytes (vs ~800 in JS) — `__slots__`, lazy meta, numpy struct-of-arrays even more impactful
- Per-subgraph `RLock` enables per-subgraph dormant eviction without global locks; free-threaded Python 3.14 enables background eviction threads
- Arrow/Parquet ecosystem advantage: `pyarrow`, `polars` for cold-tier snapshots

**Roadmap impact:** Same as TS — `GraphCodec` interface, delta checkpoints, lazy hydration, NodeV0 promotion to Phase 3.x.

**Files:** `archive/docs/SESSION-serialization-memory-footprint.md`

---

## Reading Guide

**For architecture newcomers:** Start with the spec (`~/src/graphrefly/GRAPHREFLY-SPEC.md`), then this session.

**For callbag-recharge-py context:** Read the predecessor archive index in the callbag-recharge-py repo, focusing on the Python port strategy session.

---

## Archive Format

Each session file contains:
- SESSION ID and DATE
- TOPIC
- KEY DISCUSSION (reasoning, code examples, decisions)
- REJECTED ALTERNATIVES (what was considered, why not)
- KEY INSIGHTS (main takeaways)
- FILES CHANGED (implementation side effects)

---

**Created:** March 27, 2026
**Updated:** March 31, 2026
**Archive Status:** Active — spec design + access control + cross-repo implementation audit (companion) + Tier 2 parity + demo & test strategy + universal reduction layer + serialization/memory footprint
