# Data model and storage

`aiecon schema export --out DIR` writes one JSON Schema file per contract. This page is the
map. Field-level definitions live in `src/aiecon/spec/`.

## Identifiers and lifecycle

| id | meaning |
| --- | --- |
| `workflow_id` / `workflow_run_id` | workflow type / one business task |
| `node_id` / `node_run_id` | node type / one logical execution of that node |
| `call_id` | one physical provider attempt; retries and fallbacks get new ids |
| `retry_of_call_id`, `fallback_of_call_id` | explicit lineage, never guessed from timing |
| `scope_id` | local name for a provider project / workspace |
| `dataset_id`, `data_kind` | isolates demo vs live and different imports; `synthetic` never mixes with `live` |

Calls without workflow/node labels are `unattributed`; they still count in totals.

## Raw envelope (append-only JSONL)

`RawEnvelope` carries `event_type` in {`call_started`, `call_finished`, `outcome`}, a
`revision`, a typed `payload`, and only allowlisted fields: integer metrics, timestamps,
enums, model ids, opaque ids, fingerprints. Unknown fields are rejected; content-bearing
fields never exist in the schema.

Replay rules (`aiecon.ingest`):

- same `event_id`, same content -> duplicate (no-op); different content -> conflict, the
  original stays, the CLI exits 3
- a higher `revision` for the same call or run replaces the projection; lower is superseded
- a finish before its start, or a start after a finish, never regresses the projection
- an unterminated last line is `truncated_tail`; damaged lines are counted by line number
  and error type, never echoed

## The five core tables (DuckDB, `src/aiecon/schema.sql`)

| table | key | content |
| --- | --- | --- |
| `calls` | `(dataset_id, call_id)` | `ModelCall`: current projection of one attempt, start/finish merged, mutually exclusive input token classes |
| `outcomes` | `(dataset_id, workflow_run_id)` | highest applied revision of the run outcome and per-call dispositions |
| `provider_records` | `(snapshot_id, record_id)` | provider usage / cost lines at their real grain with amount, unit, currency, USD value, source ref and hash |
| `cost_line_items` | `(dataset_id, call_id, resource, pricing_run_id)` | quantity, unit price, line cost or an unpriced reason, price id, catalog version |
| `reconciliation_buckets` | `(reconcile_run_id, bucket_key, comparison_kind)` | local vs provider values, variance, status, reasons, explained adjustments, hypotheses |

Every core row also stores its full JSON document (`doc_json`) so the pydantic model can be
rebuilt exactly.

Bookkeeping tables (`meta_*`) hold processed event hashes, ingested file hashes, snapshot
activation, pricing/reconcile runs and the catalog document a pricing run used. Views:
`call_costs`, `workflow_runs`, `node_runs`, `current_cost_line_items`,
`current_provider_records`, `current_pricing_run`.

## Token classes

```
input_total = input_uncached + input_cache_read + input_cache_write
```

Unknown values are `null`. `usage_completeness` is `complete`, `partial`, `missing` or
`invalid`; only an adapter's explicit contract may turn an absent field into a zero (see
`docs/provider-assumptions.md`). `cache_write_breakdown` keeps provider tiers
(`ephemeral_5m`, `ephemeral_1h`) mutually exclusive.

## Amount provenance

| class | meaning |
| --- | --- |
| A | provider cost or settlement data at its real grain, with `finality` |
| B | provider-reported usage x verified catalog price (every estimate in v0.1) |
| C | measured runtime resource usage (reserved, no tables) |
| D | estimated usage or assumption-laden computation (e.g. estimated prefix tokens) |
| E | heuristic allocation (not produced) |

`reconciliation_status` is a separate comparison result. Matching a day never upgrades that
day's call estimates from B to A.

## Runtime placeholders

`spec/runtime.py` defines `RuntimeExecution`, `PrefillUsage`, `DecodeUsage`, `CacheEvent`,
`GPUUsage`, `MemoryResidency` and `ResourceCost` as `x-experimental` schemas only. No table,
collector or GPU cost exists for them in v0.1.
