---
SESSION: demo-test-strategy
DATE: March 30, 2026
TOPIC: Demo & test strategy for domain layers — 4 demos, GraphReFly-powered three-pane shell, inspection-as-test-harness, foreseen building blocks
REPO: graphrefly-py (companion), graphrefly-ts (canonical)
---

## CONTEXT

Phase 4 domain layers (4.1 orchestration complete, 4.2–4.5 in progress in both TS and PY repos). This session designed the demo and test strategy for domain layer validation.

**Canonical document:** `~/src/graphrefly-ts/docs/demo-and-test-strategy.md` — full strategy with all acceptance criteria. This file summarizes Python-specific aspects.

---

## PYTHON-SPECIFIC DECISIONS

### Demos run in Pyodide/WASM lab

Python demos run in the `graphrefly-py/website/` Pyodide lab. Each mirrors the TS demo's graph topology using Python APIs. Visual layer is simplified (text/table output in the Pyodide REPL rather than full interactive UI).

| Demo | Layers | Notes |
|------|--------|-------|
| Order Processing Pipeline | 4.1+4.2+4.5+1.5 | Headless + text output |
| Multi-Agent Task Board | 4.1+4.3+4.4+3.2b+1.5 | Mock LLM (no WebLLM in Python) |
| Real-Time Monitoring Dashboard | 4.1+4.2+4.3+3.1+3.2 | Thread-safe stress test |
| AI Documentation Assistant | 4.3+4.4+3.2b+3.2+3.1 | Mock LLM |

### Scenario tests use pytest

```
tests/scenarios/
├── test_order_pipeline.py
├── test_agent_task_board.py
├── test_monitoring_dashboard.py
└── test_docs_assistant.py
```

Each mirrors the TS demo AC list, adapted for Python APIs (`snake_case`, context managers, thread safety assertions).

### Thread-safety is a first-class test axis

Python's per-subgraph `RLock` concurrency model adds a dimension absent from TS:
- Cross-factory composition under concurrent writes
- `snapshot()` during batch drain with thread contention
- `collection()` eviction while another thread reads derived nodes
- Free-threaded Python 3.14 compatibility for all adversarial tests

### Foreseen building blocks (Python-specific)

Same 6 blocks as TS (reactive cursor, streaming convention, factory helper, guard-aware describe, mock LLM, time simulation) plus:
- **Thread-safe mock LLM** — `mock_llm()` must be safe for concurrent access from multiple agent loop threads
- **Pyodide compat** — some primitives (file checkpoint, `from_cron` with real timers) need Pyodide-safe fallbacks

---

## ROADMAP IMPACT

- New Phase 7.1: Showcase demos (Pyodide/WASM lab)
- New Phase 7.2: Scenario tests
- New Phase 7.3: Inspection stress & adversarial tests (includes thread-safety)
- New Phase 7.4: Foreseen building blocks

---

## FILES

- `~/src/graphrefly-ts/docs/demo-and-test-strategy.md` — canonical strategy document
- `docs/roadmap.md` — updated Phase 7
- `archive/docs/SESSION-demo-test-strategy.md` — this file
- `archive/docs/DESIGN-ARCHIVE-INDEX.md` — updated index
