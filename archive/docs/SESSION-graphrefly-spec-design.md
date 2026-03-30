---
SESSION: graphrefly-spec-design
DATE: March 27, 2026
TOPIC: GraphReFly unified spec — protocol, single primitive (node), Graph container, cross-repo (TS + Python)
REPO: Cross-repo session (originated in callbag-recharge TS, applies to graphrefly-ts + graphrefly-py)
PREDECESSOR: callbag-recharge (TS, 170+ modules) + callbag-recharge-py (Python, Phase 0-1)
---

## CONTEXT

GraphReFly is the successor to callbag-recharge. The spec was designed through a structured
7-step process auditing lessons from 170+ TS modules and the Python port, then radically
simplifying the architecture.

## KEY DISCUSSION

### Strategic 7-step spec design process

0. **Lessons learned** — audited callbag-recharge TS (170+ modules, 20+ design sessions) and Python (Phase 0-1, 100+ tests) for what worked/failed/diverged
1. **Demands & gaps** — 7 demands (human+LLM co-operation, persistent graphs, inspectability, graphs-as-solutions, modularity, real-time, language-agnostic), 12 gaps identified
2. **Functionalities** — 10 concrete capabilities mapped to demands
3. **Common patterns** — 8 cross-cutting patterns (source→transform→sink, companion metadata, builder→graph, two-phase transition, boundary bridge, introspection, lifecycle propagation, scope isolation)
4. **Basic primitives** — progressively simplified from 6 → 5 → 1 primitive
5. **Nice-to-haves** — versioning (V0-V3), subgraph ops, LLM surface, observability, distribution
6. **Scenario validation** — 7 real-world scenarios stress-tested (LLM cost control, security policy, human-in-the-loop, Excel calculations, multi-agent routing, LLM graph building, git versioning)

### Simplification journey (from callbag-recharge)

| What | callbag-recharge | GraphReFly | Why |
|------|-----------------|------------|-----|
| Protocol | 4 callbag types (START=0, DATA=1, END=2, STATE=3) | Unified `[[Type, Data?], ...]` always-array | No channel separation needed with typed interfaces |
| Primitives | 6 (state, derived, dynamicDerived, producer, operator, effect) | 1 (`node`) + sugar constructors | All are variations of "node with optional deps and fn" |
| Introspection | Inspector + observe() + inspect() + knobs() + gauges() + namespace() | `Graph.describe()` + `Graph.observe()` | 2 methods replace 6+ concepts |
| Metadata | `with*()` wrappers (withStatus, withBreaker, etc.) | `meta` companion stores on every node | Each key is subscribable |
| Control points | Separate Knob/Gauge types | Metadata on existing nodes | No new types needed |
| Edge transforms | Considered `connect(a, b, { transform })` | Pure wire edges only | Everything is a node |
| Namespacing | namespace() utility, GraphRegistry | Colon-delimited paths (`system:payment:validate`) | String parsing, no extra primitives |
| Termination | END (overloaded: clean + error) | COMPLETE + ERROR (explicit) | No ambiguity |

### Core architectural decisions

#### 1. One primitive: `node(deps?, fn?, opts?)`
Behavior determined by configuration:
- No deps, no fn → manual source (sugar: `state()`)
- No deps, with fn → auto source (sugar: `producer()`)
- Deps, fn returns value → reactive compute (sugar: `derived()`)
- Deps, fn uses `down()` → full protocol (sugar: `operator()`)
- Deps, fn returns nothing → side effect (sugar: `effect()`)

#### 2. Unified node interface
```
node.get()          — cached value (never errors, even disconnected)
node.status         — "disconnected" | "dirty" | "settled" | "resolved" | "completed" | "errored"
node.down(msgs)     — send downstream
node.up(msgs)       — send upstream
node.unsubscribe()  — disconnect from deps
node.meta           — companion stores (each key subscribable)
```

#### 3. Always-array message format
No single-message shorthand. Always `[[Type, Data?], ...]`.

#### 4. Meta as companion stores
Each key in meta option becomes a subscribable node. Replaces all `with_*()` wrappers.

#### 5. Graph container
Named, inspectable, composable. Colon-delimited namespace. Pure wire edges.
`describe()` for structure, `observe()` for live stream.

### Python-specific advantages carried forward

- **Typed Protocol classes** — better than integer tags for IDE support, mypy
- **Unlimited-precision bitmask** — Python int, no Uint32Array fallback
- **Per-subgraph RLock** — true parallelism on independent subgraphs
- **Core is 100% synchronous** — no asyncio required for basic state
- **Context managers** — `with batch():`, `with subscribe() as sub:`
- **Pipe operator** — `source | map(fn) | filter(pred)` via `__or__`
- **Free-threaded Python 3.14 ready** — correct by design under both GIL modes

### Vision

GraphReFly = reactive graph protocol where human + LLM are peers operating on the same graph.
~8 concepts for an LLM to learn, enough to build anything.

## REJECTED ALTERNATIVES

- **Keep callbag 4-type system** — unnecessary with typed interfaces
- **5+ separate primitives** — all variations of one concept
- **Separate Knob/Gauge types** — just metadata on nodes
- **dynamicDerived as primitive** — Python lesson: declare superset, track at runtime
- **get() pull-recomputes** — return cached, status tells truth
- **Transforms on edges** — everything is a node
- **Evolve callbag-recharge-py** — too different, clean break

## KEY INSIGHTS

1. Python's superset-deps-at-construction lesson merged dynamicDerived into derived (then into node).
2. Python Protocol classes + Enum are the right encoding for the message protocol.
3. Per-subgraph concurrency model carries forward — spec defines logical model, Python adds real threading.
4. Context managers for batch and subscribe are idiomatic and should stay.
5. The spec fits ~350 lines. ~8 concepts to build anything.

## FILES CREATED / CHANGED

**graphrefly-py:**
- `~/src/graphrefly/GRAPHREFLY-SPEC.md` — the unified spec (v0.1.0 draft)
- `docs/roadmap.md` — implementation roadmap
- `archive/docs/SESSION-graphrefly-spec-design.md` — this session
- `archive/docs/DESIGN-ARCHIVE-INDEX.md` — design archive index

---END SESSION---
