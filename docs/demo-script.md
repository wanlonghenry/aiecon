# Three-minute demo script

Everything below runs offline on the packaged synthetic dataset. Say "synthetic" out loud
whenever a number appears; the report banner says it too.

## 0:00 — Fresh clone to report (three commands)

```bash
git clone https://github.com/wanlonghenry/aiecon.git && cd aiecon
uv sync --locked
uv run aiecon demo --out .aiecon/demo
```

The demo prints the stages it ran (`fixtures_loaded, ingested, estimated,
provider_fixtures_imported, reconciled, reported`), the call and outcome counts against the
committed expectations (313 calls, 100 outcomes), four shareable reconciliation lines and
the absolute paths of `report.html`, `report.json` and `report-manifest.json`. No API key,
no network.

## 0:40 — Data source check (30 seconds)

Open `.aiecon/demo/report-manifest.json`. Point at:

- `pricing_run_id` and `reconcile_run_id`: the exact runs the report was built from
- `input_file_hashes`: the SHA-256 of the ingested `events.jsonl` and of every provider
  snapshot; compare `events.jsonl` with `src/aiecon/data/demo_support_v1/events.jsonl`
- `report_json_sha256`: the JSON the HTML was rendered from

Then open `report.html`. Section 1 repeats the data kind, window, pricing run and the
number of provider snapshots with their finality (`provisional`).

## 1:10 — Outcome economics (20 seconds)

Section 2: 100 terminal runs, 90 succeeded, 10 failed. Cost per successful outcome divides
the cohort's known cost by 90 successes and is flagged as a **known-cost lower bound**
because 5 calls (timeouts and a failed primary) have no usage and therefore unknown cost.
Failed runs are inside the numerator, on purpose.

## 1:30 — Reconciliation (40 seconds)

Section 3, four buckets, one per provider and UTC day:

- OpenAI, both days: `matched`; E equals B to the cent.
- Anthropic, day 1: `variance`, fully **explained**. The usage comparison (section 4) shows
  the provider counted 20,000 uncached and 4,000 output tokens that the local log never
  captured; priced at this bucket's own rates that is exactly the $0.072 gap. Open the
  evidence toggle: the adjustment references the provider usage records.
- Anthropic, day 2: `variance`, **unexplained**: usage matches, cost differs by $0.0137,
  no evidence adjusts it. That is what an honest gap looks like.

Read one shareable line: "Your estimates run 2.8% below provider-reported costs
(anthropic, demo_scope_anthropic, 2026-09-27 UTC)." The percentage is the bucket's own
`variance_pct`; nothing was calibrated to make it smaller.

## 2:10 — One finding with evidence (30 seconds)

Section 5: expand a `retry_waste.discarded_attempt` finding. It names the run, the node
run, the superseded call and the later attempt, lists the line items and the raw event ids,
states the observed cost, and — because the application labeled the attempt as removable
in a scenario — a modeled saving equal to that cost. Then expand a `failed_run` finding:
observed cost, savings `Unknown`. Failure does not make spend avoidable.

## 2:40 — Context economics and the summary (20 seconds)

Section 6, group A: a 3,000-token prefix appeared in 43 calls; 42 with complete usage.
Provider-reported cache reads: 0. Modeled 5-minute cache scenario: $0.252 uncached vs
$0.069 cached, savings $0.183, break-even at 2 reuses. Group B shows the opposite: reads
already cover the prefix, so it is `already_cached` and no saving is claimed.

Section 7: the union of everything the findings touch, priced once, next to the single best
scenario. Joint savings say `not computed`, and they mean it.
