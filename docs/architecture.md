# Architecture

One package, one CLI, one DuckDB file, one report. Every command goes through
`aiecon.pipeline`, which opens the workspace under a single-writer lock and calls pure
functions that receive their inputs and the clock explicitly.

## Modules

| module | responsibility |
| --- | --- |
| `spec/` | pydantic contracts: envelope, call, outcome, provider record, price catalog, line item, reconciliation bucket, finding, context group, report; JSON Schema export |
| `privacy.py` | allowlist construction, HMAC prefix fingerprints, log-safe values, exception classification |
| `collect/` | JSONL writer, per-attempt LiteLLM collector, spend fuse |
| `adapters/` | usage normalization per `usage_format`; streaming final-usage rules |
| `ingest.py` | replayable projection of raw events into `calls` and `outcomes` |
| `storage.py`, `schema.sql` | workspace layout, lock, DuckDB tables and views, batched writes |
| `pricing/` | catalog index, pure `estimate()`, deterministic pricing runs |
| `billing/` | file import, unit conversion, read-only OpenAI / Anthropic pollers, staging then activation |
| `reconcile.py` | usage and cost buckets per provider, scope and UTC day; evidence adjustments; shareable line |
| `context_econ.py` | prefix groups, observed cache usage, TTL segments, section 8.3 scenarios |
| `detectors/` | discarded attempts, failed runs, unused fallbacks, repeated context; union summary; outcome economics |
| `report.py`, `templates/report.html.j2` | Report model → JSON and self-contained HTML; report manifest |
| `pipeline.py`, `cli.py` | orchestration and exit codes |
| `data/` | synthetic generator and the committed fixtures it reproduces byte for byte |

## Data flow

```
LiteLLM deployment hooks ──► EnvelopeCollector ──► JSONL (raw/YYYY-MM-DD/*.jsonl)
application outcomes ─────► JsonlWriter ─────────┘
                                                   │ aiecon ingest (idempotent)
                                                   ▼
                                     calls, outcomes (DuckDB projection)
                                                   │ aiecon estimate + catalog
                                                   ▼
                                     cost_line_items (pricing run, catalog persisted)
provider API / CSV / JSON ──► staging ──► provider_records (every snapshot kept; latest per day wins)
                                                   │ aiecon reconcile
                                                   ▼
                                     reconciliation_buckets (usage + cost, evidence)
                                                   │ aiecon report
                                                   ▼
                        findings + context economics + outcome economics ──► report.json/html
```

## Invariants the code enforces

- Unknown fields in an envelope are rejected; content-bearing fields do not exist.
- Money is `Decimal` in Python, a decimal string in JSON, `DECIMAL(24,12)` in DuckDB.
- Same event id + same content is a duplicate; different content is a conflict (exit 3).
- Estimation is pure: same call and catalog → same line items and ids.
- A pricing run id is a function of its inputs; re-running replaces, never accumulates.
- Only complete snapshots are registered, and every one stays on file. Which snapshot
  supplies a (provider, scope, record kind, UTC day) is decided at read time: the latest
  complete fetch covering that day. Re-importing a snapshot id with the same bytes is a
  no-op; with different bytes it is a contract error.
- Daily provider grains are compared over whole UTC days only; a partial-day window is
  refused rather than compared.
- A bucket whose local estimate is a known-cost lower bound is never `matched` by tolerance.
- A capture gap is priced per model with that model's own line-item rates; it is evidence
  only when every snapshot feeding the day declares the scope dedicated to the captured
  traffic.
- A report refuses to mix runs: the active pricing run must match the calls now in the
  dataset and the active reconcile run must match the pricing run and the snapshots on
  file, or the render is refused (`--allow-stale` renders with a banner).
- Estimates are never changed to match provider totals; explained variance requires
  evidence refs; the remainder is `unexplained`.
- Findings de-duplicate by line item; joint savings are not computed.
- Synthetic data carries `data_kind=synthetic` everywhere and cannot share a workspace
  with live data.
