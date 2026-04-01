# Adapter Behavior Contract

> Canonical cross-language contract for GraphReFly I/O adapters (`from_webhook`, `from_websocket`, `to_websocket`, `from_fs_watch`, `from_http`, etc.).
> Resolved as **decision K** in `docs/optimizations.md`. Keep this document in sync across both `graphrefly-ts` and `graphrefly-py`.

---

## Four pillars

### 1. Register callback expectations

| Rule | Detail |
|------|--------|
| Cleanup return | `register` **must** return a cleanup callable. Registration is atomic — the cleanup callable is valid immediately after `register` returns. |
| Optional cleanup (`from_webhook` only) | `from_webhook` permits `register` to return `None` (no cleanup needed). `from_websocket` **requires** a callable (raises on `None`). |
| Registration errors | Errors raised during `register` are forwarded as `[(ERROR, err)]` — never swallowed, never re-raised to the caller. |

### 2. Terminal-time ordering

| Rule | Detail |
|------|--------|
| Cleanup before terminal | Cleanup runs **before** the terminal tuple (`COMPLETE` / `ERROR`) is emitted downstream. Listeners are detached before terminal propagates. |
| Active guard | After cleanup, the `emit` / `error` / `complete` callbacks become no-ops (guarded by an `active` flag). |

### 3. Sink transport failure handling

| Rule | Detail |
|------|--------|
| Surface as ERROR | Transport exceptions (e.g. `socket.send` / `socket.close` failures) surface as `[(ERROR, err)]` — never swallowed, never raised to the caller. |
| Non-throwing callbacks | Callback payloads (`emit`, `error`, `complete`) are structured and non-raising by contract. Parse errors during `emit` terminate the adapter with `ERROR`. |
| `to_websocket` transport errors | Reported via optional `on_transport_error` hook. Transport failures do **not** crash the graph. |

### 4. Idempotency

| Rule | Detail |
|------|--------|
| First terminal wins | Repeated terminal input (multiple `COMPLETE` / `ERROR`) is idempotent — the first terminal wins, subsequent calls are no-ops. |
| Malformed input | Malformed or late input after terminal is silently ignored (no crash). |
| Post-terminal emit | `emit()` after terminal is a no-op. |

---

## Adapter-specific contracts

### WebSocket lifecycle (decision J1)

- **Eager terminal teardown:** On `COMPLETE` or `ERROR`, cleanup runs immediately (listeners detached, socket optionally closed).
- **Propagate sink errors:** `from_websocket` parse errors and socket event errors both surface as `ERROR` tuples, never swallowed.
- **`close_on_cleanup`:** Optional — when enabled, `socket.close()` is called during cleanup.

### Filesystem watch (decision L)

- **Debounce-only:** No polling fallback. Event-driven watcher backends only (`watchdog`).
- **Dual-path glob matching:** Globs match against both the absolute path and the watch-root-relative path.
- **Expanded payload shape:** `{ "type", "path", "root", "relative_path", "src_path"?, "dest_path"?, "timestamp_ns" }`.
- **Rename-aware:** `rename` events include `src_path` and `dest_path` when available.
- **Error via protocol:** Watcher errors emit `[(ERROR, err)]` — no raised exceptions.

### Adapter output model

- **No `async def` in public returns.** All adapters return `Node[T]` (or unsubscribe callables). Async work is wrapped inside reactive sources internally.
- **`from_any`** is the canonical bridge for unknown async shapes into the graph.

---

## Enforcement

Both repos maintain mirrored integration tests verifying the four pillars:

| Repo | Test file |
|------|-----------|
| `graphrefly-ts` | `src/__tests__/adapter-contract.test.ts` |
| `graphrefly-py` | `tests/test_adapter_contract.py` |
