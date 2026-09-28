# Integration guide

aiecon has one collection entry point (a LiteLLM callback) and one provider-report entry
point per provider (read-only report APIs or a normalized file import). Everything else is
local: raw JSONL, one DuckDB file, JSON/HTML output.

## 1. Collection through the LiteLLM proxy

```bash
uv sync --locked --extra live                      # pins litellm[proxy]==1.102.1
cp .env.example .env                               # fill in the keys you have
uv run --env-file .env aiecon --workspace .aiecon/live init        # live workspace
uv run --env-file .env aiecon --workspace .aiecon/live doctor --mode live
uv run --env-file .env litellm --config examples/litellm/config.yaml --port 4000
```

`uv run --env-file .env` loads the keys for that one process; nothing else in aiecon reads
`.env`. The same scope id must be used everywhere: `AIECON_SCOPE_ID` on the collector side,
`--scope-id` on `billing sync` (or `scope_id` in an import manifest), and `--filter` must
name the provider project / workspace that carries exactly that traffic.

`examples/litellm/config.yaml` defines two routes, `openai-live` and `anthropic-live`,
whose real model ids come from `AIECON_OPENAI_MODEL` and `AIECON_ANTHROPIC_MODEL`. Router
retries and fallbacks are disabled so that every paid attempt is one recorded call.

The callback (`examples/litellm/custom_callbacks.py` -> `aiecon.collect.litellm_callback`)
uses the three per-attempt deployment hooks documented for LiteLLM 1.102.1:

| hook | what aiecon writes |
| --- | --- |
| `async_pre_call_deployment_hook` | `call_started` with a fresh `call_id` (or the id your app supplied) |
| `async_post_call_success_deployment_hook` | `call_finished` with allowlisted usage |
| `async_post_call_failure_deployment_hook` | `call_finished` with `status` and an `error_class` code |

Envelopes land in `<workspace>/raw/YYYY-MM-DD/events-<pid>.jsonl`, one JSON object per line,
flushed on every write. Environment: `AIECON_WORKSPACE`, `AIECON_DATASET_ID` (default
`live-demo`), `AIECON_SCOPE_ID` (default `litellm_proxy`).

### What your application sends

Put business context under `metadata.aiecon` in the request body (the proxy forwards it in
`litellm_params.metadata`):

```json
{
  "model": "anthropic-live",
  "messages": [...],
  "metadata": {
    "aiecon": {
      "workflow_id": "support_agent",
      "workflow_run_id": "run_2026_09_27_0001",
      "node_id": "reasoning",
      "node_run_id": "run_2026_09_27_0001_reasoning",
      "call_id": "call_optional_client_supplied_id",
      "scope_id": "live_anthropic_workspace",
      "cache_policy": "ephemeral_5m",
      "prefix_fingerprint": "<hmac-sha256 hex of the stable prefix>",
      "fingerprint_key_id": "fpk_...",
      "prefix_tokens": 5100,
      "prefix_token_count_method": "provider_count_tokens"
    }
  }
}
```

Only opaque ids and controlled codes are accepted; anything else is dropped. Lineage is
derived from `node_run_id`: a later attempt for the same node run is a *retry* when the
model group is unchanged and a *fallback* otherwise. Nothing is inferred from timing.

Outcomes (`succeeded/failed/abandoned`, per-call `used/discarded` dispositions) are written
by the application, not by the proxy. `examples/run_workload.py` shows the shape; it writes
`source_type=application_sdk` envelopes into the same `raw/` directory.

### Prefix fingerprints

`aiecon.privacy.prefix_fingerprint(key, parts)` computes an HMAC-SHA256 over an ordered,
length-prefixed list of prefix parts (tools, system, leading messages, rendering config).
The key comes from `AIECON_FINGERPRINT_KEY` and is identified in the data only by
`fingerprint_key_id`. Reuse is never inferred across scopes, keys or models.

## 2. The live workload

```bash
uv run --env-file .env python examples/run_workload.py --dry-run
uv run --env-file .env python examples/run_workload.py --live --budget-usd 10 --yes-spend
uv run --env-file .env python examples/run_workload.py --live --budget-usd 10 --yes-spend --providers anthropic
```

Dry run prints the 18-call plan with a conservative reservation per call and sends
nothing. Live mode reserves before each call, settles with the computed cost after, keeps
the reservation and stops when a cost cannot be computed, and persists its state in
`<workspace>/state/spend.json` so a restart continues from the same totals. CI never runs it.

## 3. Local pipeline

```bash
uv run aiecon --workspace .aiecon/live ingest --input .aiecon/live/raw
uv run aiecon --workspace .aiecon/live estimate --catalog catalogs/live-demo.json
uv run --env-file .env aiecon --workspace .aiecon/live billing sync --provider openai \
    --scope-id live_openai_project --filter <openai project id> --dedicated-scope \
    --start 2026-09-27 --end 2026-09-29
uv run --env-file .env aiecon --workspace .aiecon/live billing sync --provider anthropic \
    --scope-id live_anthropic_workspace --filter <anthropic workspace id> --dedicated-scope \
    --start 2026-09-27 --end 2026-09-29
uv run aiecon --workspace .aiecon/live reconcile --start 2026-09-27 --end 2026-09-29
uv run aiecon --workspace .aiecon/live report --out .aiecon/live/reports/report.html
```

The `--scope-id` values are the ones the calls carry: `examples/run_workload.py` sends
`live_openai_project` and `live_anthropic_workspace` in the request metadata (its
`--scope-openai` / `--scope-anthropic` options), and the collector falls back to
`AIECON_SCOPE_ID` for requests without one. Without matching scope ids every bucket is
`scope_mismatch`.

`billing sync` needs `OPENAI_ADMIN_API_KEY` / `ANTHROPIC_ADMIN_API_KEY` (admin keys, not
model keys). It fetches every page first, stages `records.json` + `manifest.json` under
`<workspace>/provider/sync/<snapshot_id>/`, then imports the snapshot. A failed pull leaves
earlier snapshots untouched. Use `--filter <project or workspace id>` so the provider side
matches the scope your calls carry; comparing an organisation-wide export against one
project's calls yields `scope_mismatch`, not a number. Add `--dedicated-scope` only when
that project / workspace carries nothing but the traffic aiecon captured: it lets a
provider-side surplus count as an evidence-backed capture gap instead of a hypothesis.

Dates are UTC; `--end` is exclusive; `reconcile` accepts whole UTC days only when daily
provider grains are involved. Re-pull the last three days regularly: provider data is
provisional and may be revised. Every pull is kept; for each provider, scope, record kind
and UTC day the latest complete pull covering that day is the one compared, so a one-day
re-pull replaces that day only and never hides the others.

## 4. File import instead of the API

```bash
uv run aiecon --workspace .aiecon/live billing import --file export.csv --manifest export.manifest.json
```

CSV columns, in this order:

```
record_id,window_start_ms,window_end_ms,dimensions_json,amount_original,amount_unit,currency,usage_json
```

JSON uses the same fields inside `{"records": [...]}`; `dimensions_json` and `usage_json`
may be JSON strings or objects. Usage records leave the amount fields empty; cost records
leave `usage_json` empty. Amount units: `usd` (currency units) or `cents`. The manifest:

```json
{
  "schema_version": "0.1",
  "snapshot_id": "snap_openai_cost_2026-09-27",
  "provider": "openai",
  "scope_id": "live_openai_project",
  "record_kind": "provider_cost",
  "data_kind": "live",
  "grain": "1d/line_item,project_id",
  "query_window": {"start_ms": 1790467200000, "end_ms": 1790640000000},
  "fetched_at_ms": 1790650000000,
  "source_ref": "console export costs.csv downloaded 2026-09-29 for project proj_x",
  "source_hash": "<sha256 of the file>",
  "finality": "provisional",
  "snapshot_complete": true
}
```

`source_hash` must equal the file's SHA-256. Re-importing the same `snapshot_id` with the
same bytes is a no-op; the same id with different bytes is refused (use a new id for a new
pull). A newer complete snapshot supplies the UTC days its `query_window` covers; older
snapshots keep supplying the days it does not. `scope_dedicated: true` may be added when the
provider scope carries only the captured traffic. `record_kind` is `provider_usage`,
`provider_cost` or `settled_cost`; nothing becomes "settled" by being imported.

### Console exports

The Anthropic Console *token usage* export (`claude_api_tokens_<from>_to_<to>.csv`) converts
to a `provider_usage` snapshot with `examples/anthropic_console_usage_to_aiecon.py`:

```bash
uv run python examples/anthropic_console_usage_to_aiecon.py     --csv ~/Downloads/claude_api_tokens_2026_08_30_to_2026_09_28.csv     --scope-id live_anthropic_workspace --snapshot-id snap_anthropic_usage_console_20260928     --start 2026-08-30 --end 2026-09-29 --dedicated-scope     --out .aiecon/live/provider/imports/anthropic_usage_console_20260928
uv run aiecon --workspace .aiecon/live billing import     --file .aiecon/live/provider/imports/anthropic_usage_console_20260928/records.json     --manifest .aiecon/live/provider/imports/anthropic_usage_console_20260928/manifest.json
```

Token counts are usage, not money: this yields a usage comparison only. For the monetary
comparison import the Console *cost* export (amounts; it lags the usage export by up to a
day) or use `billing sync` with an admin key.

The OpenAI dashboard *cost* export (`cost_<from>_<to>.csv`, with `amount_value` columns)
converts with `examples/openai_dashboard_cost_to_aiecon.py`:

```bash
uv run python examples/openai_dashboard_cost_to_aiecon.py     --csv ~/Downloads/cost_2026-09-25_2026-09-29.csv     --scope-id live_openai_project --snapshot-id snap_openai_cost_dashboard_20260928     --dedicated-scope --out .aiecon/live/provider/imports/openai_cost_dashboard_20260928
uv run aiecon --workspace .aiecon/live billing import     --file .aiecon/live/provider/imports/openai_cost_dashboard_20260928/records.json     --manifest .aiecon/live/provider/imports/openai_cost_dashboard_20260928/manifest.json
```

Names and e-mail addresses in the export never become dimensions. Without a project
grouping the export is organisation-wide: the local dataset must then hold every call the
organisation made in the window, and `--dedicated-scope` is only true when nothing else
ran there. An export whose rows carry no amount columns cannot be used.

## 5. Reading the results

### PDF

The HTML report carries a print stylesheet (A4, light colours, tables unclipped). Any
Chromium browser prints it; collapsed evidence blocks stay collapsed unless they are opened
first, so expand them in a copy and print that copy headlessly:

```bash
uv run python - <<'PY'
from pathlib import Path
src = Path(".aiecon/live/reports/report.html").read_text("utf-8")
Path(".aiecon/live/reports/report-print.html").write_text(src.replace("<details>", "<details open>"), "utf-8")
PY
# Windows: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"; Linux/macOS: google-chrome / chromium
msedge --headless=new --disable-gpu --no-pdf-header-footer     --print-to-pdf=.aiecon/live/reports/report.pdf .aiecon/live/reports/report-print.html
```

- `report.json` is the machine-readable interface; `report.html` is rendered from it.
- `report-manifest.json` records versions, run ids, snapshot ids, input hashes and the
  hashes of the two report files.
- `aiecon schema export --out schemas/` writes the JSON Schema of every contract.
- Exit codes: 0 done (variance is not an error), 1 execution failure, 2 configuration or
  argument error, 3 data-contract conflict (duplicate event id with different content,
  manifest hash mismatch, wrong data kind).
