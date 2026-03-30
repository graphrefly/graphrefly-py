# Session: Tier 2 Operator Parity — Signal Ordering, nonlocal, forwardInner, sample Architecture

**SESSION ID:** tier2-parity-nonlocal-forward-inner
**DATE:** March 30, 2026
**REPOS:** graphrefly-ts, graphrefly-py
**CANONICAL LOG:** `~/src/graphrefly-ts/archive/docs/SESSION-tier2-parity-nonlocal-forward-inner.md`

---

## Topic

Cross-repo parity fixes for three divergences in Python's Tier 2 operators, discovered during the `/parity` run following the COMPLETE-before-DATA batch ordering fix. All code changes are in this repo (graphrefly-py); TS was already correct.

---

## Python-Specific Changes

### 1. `_forward_inner` — emitted flag prevents double-DATA

Added `emitted` flag inside `inner_sink`. The manual `actions.down([(DATA, inner.get())])` after subscribe now only fires when subscribe didn't already deliver DATA. Prevents double-DATA for derived/computed inner nodes while preserving the manual emit needed for state nodes (which don't auto-emit on subscribe in Python).

### 2. `nonlocal` replaces `[value]` list-boxing (18 operators)

Replaced all `[False]`/`[None]`/`[0]` closure-capture patterns with idiomatic `nonlocal` across: `_forward_inner`, `switch_map`, `concat_map`, `flat_map`, `exhaust_map`, `debounce`, `throttle`, `audit`, `timeout`, `buffer_time`, `interval`, `repeat`, `pausable`, `window`, `window_count`, `window_time`.

Kept `holder: list[NodeActions | None] = [None]` in `open_win()` — one-shot callback capture, `nonlocal` doesn't help.

Moved `flush()`/`close_window()`/`fire()` out of `for m in msgs:` loops to fix ruff B023.

### 3. `sample` rewritten to dep+onMessage pattern

Eliminated the mirror node + raw subscribe architecture. Now uses `node([src, notifier], compute, on_message=...)` matching TS — source messages (index 0) swallowed, notifier DATA (index 1) triggers `src.get()` emission.

---

## Forward: Tier 2 Regression Testing Priority

Python currently lacks an equivalent to TS's `operator-protocol-matrix.test.ts` (1046 lines, 50+ operators). As we wire more message types, composite data, and adapters that RxJS/callbag never had to deal with, Tier 2 operators are the highest regression risk:

- Composite message ordering through dynamic inner subscriptions
- Void sources (`Node[None]`) through every `*_map` operator
- PAUSE/RESUME and INVALIDATE propagation through inners
- Timer callbacks firing during batch drain
- Error recovery (`rescue` wrapping `switch_map`)

**Action:** Create `tests/test_tier2_protocol_matrix.py` with heavily commented tests defending correct semantics.

---

## Files Changed

- `src/graphrefly/extra/tier2.py` — all three fixes + lint cleanup

**Cross-reference:** Follows `SESSION-cross-repo-implementation-audit.md` (signal tier system, `partition_for_batch` three-way split, `attached` flag pattern).
