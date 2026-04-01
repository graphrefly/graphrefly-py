---
SESSION: universal-reduction-layer
DATE: March 31, 2026
TOPIC: GraphReFly as a universal reduction layer — massive info to actionable items, LLM-composable graphs, observability/telemetry as one instance of the pattern
REPO: graphrefly-py (companion), graphrefly-ts (canonical)
---

## CONTEXT

Companion to the canonical session log in `~/src/graphrefly-ts/archive/docs/SESSION-universal-reduction-layer.md`. This file summarizes **Python-specific** considerations and roadmap additions.

The full design rationale, industry research (OpenTelemetry/Datadog/Grafana pain points, 2026 trends), thesis ("reactive graphs as universal reduction layer"), 10 advantages, rejected alternatives, and key insights are in the TS session doc. Read that first.

---

## PYTHON-SPECIFIC CONSIDERATIONS

### Adapter ecosystem

Python has natural advantages for several ingest/sink adapters:
- **Kafka**: `confluent-kafka` or `aiokafka` are mature
- **ClickHouse**: `clickhouse-connect` is the official client
- **S3**: `boto3` is ubiquitous
- **Prometheus**: `prometheus-client` for exposition; HTTP scrape for ingest
- **Syslog/StatsD**: stdlib `socketserver` + `struct` for UDP parsing
- **Redis Streams**: `redis-py` with `xread`/`xadd`

### Concurrency implications for high-throughput ingest

Phase 8 reduction primitives (stratify, funnel, feedback) may create graph topologies with 100+ nodes processing 10K+ msgs/sec. Python-specific concerns:
- Per-subgraph `RLock` (Phase 0.4) is critical — independent branches must not contend
- Free-threaded Python 3.14 enables true parallelism for CPU-bound reduction stages
- `asyncio` integration needed for I/O-bound adapters (Kafka consumer, HTTP receivers) via `from_awaitable`/`from_async_iter`
- `multiprocessing`-based `sharded_graph` for scaling beyond single process

### OTel bridge in Python

Python OTel SDK (`opentelemetry-sdk`) is widely deployed. `from_otel()` should accept:
- `opentelemetry.sdk.trace.SpanProcessor` interface for trace ingest
- `opentelemetry.sdk.metrics.MetricReader` interface for metric ingest
- `opentelemetry.sdk._logs.LogEmitterProvider` for log ingest

This allows teams to keep existing OTel instrumentation while adding GraphReFly as the reactive processing layer.

---

## ROADMAP ADDITIONS (PYTHON-SPECIFIC)

See the TS roadmap for full Phase 8 descriptions. Python equivalents use `snake_case` naming:

### Phase 5.2c — `from_otel`, `from_syslog`, `from_statsd`, `from_prometheus`, `from_kafka`/`to_kafka`, `from_redis_stream`/`to_redis_stream`, `from_csv`/`from_ndjson`, `from_clickhouse_watch`
### Phase 5.2d — `to_clickhouse`, `to_s3`, `to_postgres`/`to_mongo`, `to_loki`/`to_tempo`, `checkpoint_to_s3`, `checkpoint_to_redis`
### Phase 8.1 — `stratify`, `funnel`, `feedback`, `budget_gate`, `scorer`
### Phase 8.2 — `observability_graph`, `issue_tracker_graph`, `content_moderation_graph`, `data_quality_graph`
### Phase 8.3 — `GraphSpec`, `compile_spec`, `decompile_graph`, `llm_compose`, `llm_refine`, `spec_diff`
### Phase 8.4 — `audit_trail`, `explain_path`, `policy_enforcer`, `compliance_snapshot`
### Phase 8.5 — Backpressure protocol, `peer_graph`, benchmarks, `sharded_graph`, adaptive sampling

---

## FILES

- This file: `archive/docs/SESSION-universal-reduction-layer.md`
- Canonical log: `~/src/graphrefly-ts/archive/docs/SESSION-universal-reduction-layer.md`
- Roadmap updates: `docs/roadmap.md` (Phase 5.2c, 5.2d, 7.5b, 8.1–8.5)
