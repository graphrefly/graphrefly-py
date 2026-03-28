# Benchmarks

Python mirrors the graphrefly-ts / callbag-style suite using [pytest-benchmark](https://pytest-benchmark.readthedocs.io/). Scenarios match [`tests/bench_core.py`](../tests/bench_core.py) and graphrefly-ts `src/__bench__/graphrefly.bench.ts` (aligned with callbag-recharge `compare.bench.ts`).

## Default test run

Regular `pytest` skips benchmark tests so CI and local runs stay fast:

```bash
uv sync
uv run pytest
```

This applies `-m 'not benchmark'` from [`pyproject.toml`](../pyproject.toml).

## Run benchmarks

```bash
uv run pytest tests/bench_core.py -m benchmark --benchmark-only
```

## Baseline JSON

The committed file [`benchmarks/py-baseline.json`](../benchmarks/py-baseline.json) is produced with:

```bash
uv run pytest tests/bench_core.py -m benchmark --benchmark-only --benchmark-json=benchmarks/py-baseline.json
```

Numbers are machine-specific. The file is a **snapshot** for human review and future tooling; pytest-benchmark’s compare workflows usually use the `.benchmarks/` directory on a single machine.

## Other checks

- Loose wall-clock guard (timing only when `CI` is unset): [`tests/test_perf_smoke.py`](../tests/test_perf_smoke.py)

## Setup note

Each benchmark builds its graph once per test function, then `benchmark(...)` runs the hot loop only. That matches the TS pattern of defining graphs inside each `describe` block at load time.
