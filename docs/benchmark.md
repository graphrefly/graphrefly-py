# Benchmarks

Python mirrors the graphrefly-ts micro-benchmarks in [`src/__bench__/graphrefly.bench.ts`](https://github.com/nicepkg/graphrefly-ts/blob/main/src/__bench__/graphrefly.bench.ts) using [pytest-benchmark](https://pytest-benchmark.readthedocs.io/).

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

## Sources

- Bench module: [`tests/bench_core.py`](../tests/bench_core.py)
- Loose wall-clock guard (timing only when `CI` is unset): [`tests/test_perf_smoke.py`](../tests/test_perf_smoke.py)

## Setup note

Like the TypeScript benches, graphs are built at **import time** so the hot loops run against initialized nodes.
