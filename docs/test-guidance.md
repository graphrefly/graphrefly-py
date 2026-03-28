# Test guidance

Guidelines for writing, organizing, and maintaining tests in **graphrefly-py**. Read this before adding tests. Behavioral truth is **`docs/GRAPHREFLY-SPEC.md`**; this file adds Python-specific testing patterns.

**Predecessor:** Patterns and stress tests in `~/src/callbag-recharge-py` are a valuable reference when porting operators and concurrency — adapt to GraphReFly **Messages** and lifecycle types.

---

## Guiding principles

1. **Verify before fixing.** Every suspected bug needs a failing test (or a new test that proves the hypothesis wrong). Do not fix without evidence.

2. **Spec and code > old tests.** When a new test disagrees with an older test, use **`GRAPHREFLY-SPEC.md`** and source code to decide. Update the wrong artifact.

3. **Design choices ≠ bugs.** Documented invariants (e.g. batch defers **DATA** not **DIRTY**; **RESOLVED** for unchanged recompute) are intentional. Check the spec before “fixing” behavior.

4. **Test what the code should do.** Write tests for correct semantics; failures indicate real bugs.

5. **One concern per test.** Avoid combining unrelated scenarios in one `test_*` function.

6. **Prefer helpers over raw wiring.** When the project provides observation helpers (message capture, fake sinks), use them for assertions. Reserve hand-built sinks for low-level protocol tests.

7. **Authority hierarchy for expected behavior:**
   - `docs/GRAPHREFLY-SPEC.md` — primary
   - `docs/roadmap.md` — phase acceptance
   - `~/src/callbag-recharge-py` — porting reference only

---

## Test file organization (evolve with roadmap)

Start minimal; split as features land:

```
tests/
├── conftest.py              — shared fixtures
├── test_smoke.py            — package / import sanity
├── test_protocol.py         — message types, invariants, batch semantics (Phase 0.2)
├── test_core.py             — node primitive, sugar, diamond, lifecycle (Phase 0.3+)
├── test_concurrency.py      — locks, threads, free-threaded concerns (Phase 0.4)
├── test_graph.py            — Graph container, mount, describe, observe, signal, destroy, snapshot/restore (Phase 1)
├── test_guard.py            — Actor, guard, `policy()`, scoped describe/observe (Phase 1.5)
├── test_extra_tier1.py      — sync operators (Phase 2.1)
├── test_extra_tier2.py      — async/dynamic operators (Phase 2.2)
├── test_regressions.py      — regression suite (never delete entries; add date + fix note)
└── ...
```

**Rule:** Add to the narrowest existing file; create a new file when the area is orthogonal.

---

## What to test (protocol and nodes)

Aligned with **GRAPHREFLY-SPEC** and **roadmap** Phase 0:

- [ ] **Message shape:** always a list of tuples; no shorthand single message
- [ ] **Ordering:** **DIRTY** before **DATA** or **RESOLVED** in a batch/cycle as required
- [ ] **Batch:** **DIRTY** propagates during batch; **DATA** deferred to batch exit
- [ ] **Diamond:** downstream node runs **once** per logical update at convergence
- [ ] **RESOLVED:** downstream skip when upstream value unchanged after dirty
- [ ] **ERROR** / **COMPLETE:** terminal behavior; no further emissions from that source unless resubscribable
- [ ] **Forward:** unknown message types pass through
- [ ] **Threading:** where APIs claim thread-safe **get()** / propagation, stress with multiple threads (see roadmap 0.4)

---

## Diamond resolution pattern

```python
def test_diamond_computes_once():
    # Pseudocode — adapt to actual API surface (node/derived/graph) once implemented
    # a = state(1)
    # b = derived([a], lambda: a.get() * 2)
    # c = derived([a], lambda: a.get() + 10)
    # d = derived([b, c], lambda: b.get() + c.get())
    # count effect runs on d; a.set(5); assert d ran once; assert d.get() == 25
    ...
```

Always assert: (1) compute count, (2) final value.

---

## RESOLVED skip pattern

When a dependency recomputes to the same value, downstream nodes should not redo work unnecessarily — assert **RESOLVED** propagation per spec (see **GRAPHREFLY-SPEC** § protocol).

---

## Error handling

- **fn** throws → **ERROR** downstream as `[[ERROR, err]]` (or spec-equivalent), not silent completion.

---

## Reconnect / resubscribe

If nodes support resubscribe, test: unsubscribe → subscribe again → fresh state where applicable (counters, inner subscriptions).

---

## Concurrency patterns

Port ideas from **callbag-recharge-py** `test_concurrency.py` when implementing Phase 0.4:

- Concurrent **get()** without torn reads
- Independent subgraphs updated without deadlock
- Under load, **DIRTY**/**DATA** ordering invariants still hold

Always use **timeouts** and **liveness assertions** on thread joins where threads might block.

---

## Regression tests

In `test_regressions.py`, each entry should cite what broke, the fix, and a date. Never delete; only add.

---

## Running tests

```bash
uv run pytest
uv run pytest tests/test_core.py
uv run pytest tests/test_core.py::test_name -v
uv run pytest -x
```

See **`CLAUDE.md`** for lint and typecheck commands.
