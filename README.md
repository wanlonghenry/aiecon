# aiecon

> Reconstruct AI workflow costs, reconcile them with provider reports, and inspect evidence-backed waste and context reuse opportunities. Run the local demo without API keys.

[Sample report](docs/sample-report.html) (synthetic data, single self-contained HTML page)

`aiecon` is a small, fully local Python toolkit: LiteLLM telemetry in, a versioned price
catalog, one DuckDB file, and a report that answers five questions with evidence you can
trace back to individual events, line items and provider records. Status: **reproducible
synthetic demo (L0)**. Live reconciliation against real provider costs (L1) is **pending**
for both providers; nothing in this repository claims otherwise.

## Quickstart

```bash
git clone https://github.com/wanlonghenry/aiecon.git && cd aiecon
uv sync --locked
uv run aiecon demo --out .aiecon/demo
```

`git clone → uv sync --locked → uv run aiecon demo` — no API key, no network, no second
repository, and you get the complete local report. The demo ingests 313 synthetic calls
from 100 workflow runs, prices them, imports synthetic provider usage and cost snapshots,
reconciles per provider and UTC day, runs the waste detectors and writes:

```
.aiecon/demo/report.html          single-page report
.aiecon/demo/report.json          the same numbers, machine-readable
.aiecon/demo/report-manifest.json versions, run ids, input hashes
.aiecon/demo/aiecon.duckdb        rebuildable database
```

It ends with `SYNTHETIC DEMO`. Python 3.12 and [uv](https://docs.astral.sh/uv/) are the
only prerequisites.

## The five questions the report answers

1. Which workflows, nodes, models and individual calls produced cost?
2. Does locally recorded token usage agree with the provider's usage report?
3. Within the same time window, account scope and product category, how far is the local
   estimate from the provider-reported cost — and how much of the gap has evidence?
4. Which cost is tied to failures, discarded results, redundant fallbacks or repeated
   context?
5. Which improvement is worth trying first: what is the calculation, the modeled saving and
   its limits?

## Supported inputs and validation status

| Area | Supported in v0.1 | Status |
| --- | --- | --- |
| Collection | LiteLLM 1.102.1 per-attempt deployment hooks (`async_pre_call_deployment_hook`, success/failure deployment hooks); one physical attempt = one call; explicit retry/fallback lineage | fixture-verified; live pending |
| Usage formats | `openai_chat_completions`, `openai_responses` (incl. `cache_write_tokens`), `anthropic_messages` (incl. 5m/1h cache tiers), `litellm_standard` (LiteLLM-transformed) | fixture-verified (`tests/fixtures/usage/`) |
| Streaming | OpenAI final usage chunk; Anthropic cumulative `message_delta` merged with `message_start` | fixture-verified |
| Prices | `catalogs/live-demo.json`: gpt-5-nano, claude-haiku-4-5, claude-sonnet-5, retrieved 2026-09-26 from the official pricing pages; synthetic catalog for the demo | verified against docs |
| OpenAI reports | `GET /v1/organization/usage/completions`, `GET /v1/organization/costs` (admin key) | mock-transport tests; **live reconciliation pending** |
| Anthropic reports | `GET /v1/organizations/usage_report/messages`, `GET /v1/organizations/cost_report` (admin key, amounts in cents) | mock-transport tests; **live reconciliation pending** |
| File import | normalized CSV / JSON + manifest with source hash | fixture-verified |

Details, verbatim contract quotes and unverified items: [`docs/provider-assumptions.md`](docs/provider-assumptions.md).

## How it works

```
calls + outcomes ─▶ allowlist + HMAC ─▶ immutable JSONL ─▶ idempotent DuckDB projection
                                                              │
versioned price catalog ──────────────────────────▶ per-call estimate (pure function)
                                                              │
provider usage / cost snapshots ──▶ scope + grain alignment ─▶ reconciliation buckets
                                                              │
                                    detectors + context economics ─▶ report.json / report.html
```

- `aiecon ingest` replays raw JSONL idempotently: same data twice never doubles a call;
  the same event id with different content is a conflict, never a silent overwrite.
- `aiecon estimate` prices every call from a catalog with effective dates; anything it
  cannot price without guessing stays *unpriced* with a reason.
- `aiecon billing import` / `aiecon billing sync` bring in provider usage and cost as
  complete snapshots at the provider's real grain; a new snapshot replaces the old one
  as a whole.
- `aiecon reconcile` compares usage and money separately per provider, scope and UTC day,
  attaches evidence-backed adjustments and leaves the rest as `unexplained`.
- `aiecon report` renders JSON and HTML from one model; nulls are shown as unknown, never
  as `$0.00`.

Integration steps, the request metadata your application sends, the CSV contract and the
live workload script: [`docs/integration.md`](docs/integration.md). Storage layout:
[`docs/schema.md`](docs/schema.md). Pricing rules: [`docs/pricing.md`](docs/pricing.md).
Three-minute walkthrough: [`docs/demo-script.md`](docs/demo-script.md).

## What the numbers mean

- **Estimate (E)** = provider-reported usage × verified catalog price (evidence class B).
  Estimates are never overwritten to match provider totals, and matching a day never
  upgrades that day's calls to billed amounts.
- **Provider-reported cost (B)** is what the provider's cost API or export says for the same
  scope and day. It is not an invoice; `settled_cost` only exists when a settlement file was
  imported.
- **Variance** is `E − B`; the percentage is only computed when `B > 0`. Tolerances classify
  a bucket as `matched` or `variance`; they change no amounts.
- **Findings** carry observed cost, evidence ids, assumptions and caveats. Savings are
  scenarios and stay `null` unless the application labeled the call removable or a cache
  scenario is fully priced. The summary reports the *union* of flagged line items and the
  best *single* action; joint savings are not computed.
- **Cost per successful outcome** divides a terminal cohort's whole cost (failed runs
  included) by its successes and says so when it is only a known-cost lower bound.

Known gaps and deliberate non-goals: [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md).

## Development

```bash
uv run ruff check .
uv run pytest -q -m 'not live'
uv run aiecon demo --out .aiecon/demo
uv build
```

CI runs the same commands plus a wheel install check on a clean environment; it never
holds provider credentials or runs paid calls. The build plan that produced this
repository is `PLAN.md`; agent working rules are in `AGENTS.md`.

## Roadmap (not in v0.1)

Image / audio / multimodal metering units, Batch API, server-side tool fees, self-hosted
GPU costing (vLLM / SGLang metrics), joint savings across actions, automatic routing or
policy enforcement. See PLAN.md section 17.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
