---
SESSION: serialization-memory-footprint
DATE: March 31, 2026
TOPIC: Adoption blockers for the universal reduction layer — memory footprint, serialization overhead, tiered representation, and NodeV0 promotion. Python companion to canonical session in ~/src/graphrefly-ts/archive/docs/SESSION-serialization-memory-footprint.md
REPO: graphrefly-py (companion), graphrefly-ts (canonical)
---

## CONTEXT

Python companion to the canonical session. See `~/src/graphrefly-ts/archive/docs/SESSION-serialization-memory-footprint.md` for the full discussion covering DAG-CBOR as default codec, tiered representation (hot/warm/cold/peek), five memory tuning strategies, delta checkpoints, and NodeV0 promotion from Phase 6 to Phase 3.x.

This file captures Python-specific considerations only.

---

## PYTHON-SPECIFIC CONSIDERATIONS

### DAG-CBOR ecosystem

- `dag-cbor` (PyPI) — pure Python DAG-CBOR encoder/decoder, IPLD-compatible
- `cbor2` — C-accelerated CBOR, widely used (no DAG-CBOR CID tags out of box, but extensible via tag hooks)
- `zstandard` — Python bindings for zstd compression (C extension, very fast)
- Recommendation: `cbor2` with custom CID tag handler (tag 42) for performance; `dag-cbor` for strict IPLD interop

### Memory footprint differences

Python objects are heavier than JS:
- Base `object` with `__dict__`: ~200 bytes (vs ~64 bytes JS)
- `dict` (meta companion): ~400 bytes (vs ~300 bytes JS)
- Per-node overhead: **~1200-1500 bytes** in naive Python (vs ~800 bytes in JS)

This makes the memory tuning strategies even more important in Python:
- **`__slots__`** on node classes — eliminates `__dict__`, saves ~100 bytes/node
- **Lazy meta** — even more impactful in Python (~35-40% savings)
- **Struct-of-arrays via `numpy`** — typed arrays are as efficient as JS TypedArrays
- **`mmap` for FlatBuffers** — Python `mmap` module maps directly; zero-copy is native

### Concurrency implications for dormant eviction

Python's per-subgraph `RLock` (already in architecture) means dormant eviction can happen per-subgraph without global locks. A dormant subgraph serializes to DAG-CBOR, releases Python objects, and the lock is released. Re-hydration on access re-acquires the lock.

Free-threaded Python 3.14: dormant eviction can run on a background thread without GIL contention.

### Arrow/Parquet ecosystem advantage

Python has the strongest Arrow/Parquet ecosystem:
- `pyarrow` — Apache Arrow native, Parquet read/write, zero-copy IPC
- `polars` — Arrow-native DataFrame, excellent for graph-as-table operations
- `pandas` with Arrow backend — widespread

Cold-tier graph snapshots as Arrow tables are natural in Python.

### NodeV0 in Python

Same recommendation: promote from Phase 6 to Phase 3.x. Python's `dataclass` or `NamedTuple` for V0 is zero-friction:

```python
@dataclass(slots=True)
class V0:
    id: str
    version: int
```

---

## FILES

- This file: `archive/docs/SESSION-serialization-memory-footprint.md`
- Canonical session: `~/src/graphrefly-ts/archive/docs/SESSION-serialization-memory-footprint.md`
