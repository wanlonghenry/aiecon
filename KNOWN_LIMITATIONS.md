# Known limitations (v0.1.1)

Honest boundaries of this release. Items marked *pending* are planned; items marked
*non-goal* are deliberately outside v0.1 (PLAN.md sections 1.3 and 17).

## Validation

- **Live reconciliation covers one day and one provider's money.** On 2026-09-28 UTC the
  OpenAI estimate matched the dashboard cost export exactly (E = B = $0.00383598 for an
  organisation-wide export; 17 gpt-5-nano calls incl. one failed attempt with unknown cost)
  and the Anthropic token usage matched the Console usage export exactly (11
  claude-haiku-4-5-20251001 calls). Anthropic money is still `no_provider_cost` (the Console
  cost export lagged usage and had no rows) and neither provider's report *API* has been
  exercised live (no admin keys). One matched day is not an accuracy claim for other
  models, tiers or charge types.
- Both `litellm_standard` usage fixtures are live captures (2026-09-28, LiteLLM 1.102.1);
  the Anthropic one has its nested 5m/1h breakdown filled in as all-5m because the collector
  of that build dropped the nested values (recorded as schema drift, fixed since).
- OpenAI reports a dated model id (`gpt-5-nano-2025-08-07`) on non-streaming responses and
  the bare alias on streamed ones; the cost breakdown therefore shows two rows for one
  model. Both resolve to the same catalog prices.
- OpenAI cost-report latency and finalization behaviour could not be verified from an
  official page (help-center pages were unreachable); snapshots are treated as provisional.

## Data and semantics

- Reconciliation grain is the UTC day per provider and scope, plus per-model buckets only
  where the provider report carries a structured model dimension. OpenAI costs are grouped
  by `line_item` and never by model; no model cost is invented for them.
- Unmodeled provider charges (web search, code execution, images, audio, priority tier)
  are listed separately and excluded from `B`; the estimate cannot explain them.
- Calls that straddle UTC midnight are priced by their start day and flagged
  `boundary_call`; the provider may report them on the end day. This is shown as a
  hypothesis, not as evidence.
- Anthropic cache writes without a 5m/1h breakdown remain unpriced (the estimate is a
  lower bound). When a provider usage report for the same model and UTC day leaves exactly
  those tokens unaccounted for in one tier, reconciliation adds an evidence-backed
  `cache_tier` adjustment for them; the estimate itself is never changed. OpenAI cache
  writes without a counter make a write-billed model's estimate a lower bound.
- Calls with missing or invalid usage (timeouts, interrupted streams, failed primaries)
  have unknown cost. Totals that include them are labeled known-cost lower bounds, and a
  reconciliation day that contains them is never `matched` by tolerance.
- A provider-side token surplus is evidence of a capture gap only when the snapshot
  declares the scope dedicated to the captured traffic; otherwise it is a hypothesis, since
  other traffic in the same project or workspace looks identical from the provider side.
  Surplus on a model with no locally priced calls (or an unknown cache-write tier) is not
  priced at all.
- Which snapshot supplies a UTC day is the latest complete fetch covering it; a later pull
  that omits a row makes that row disappear for its days. Snapshots are never merged.
- Reasoning tokens are priced inside output tokens; no separate reasoning price exists.
- Service tiers other than `standard` and regions other than `global` are not priced.
  Anthropic US-only inference (1.1x) and OpenAI fast/flex/batch tiers are unpriced.

## Context economics

- Scenarios assume sequential calls, one write per cold-start segment, no eviction, and a
  prefix that is repeated verbatim; TTLs are fixed per policy (5 min, 60 min, and a
  conservative 5 min for OpenAI automatic caching).
- Overlapping concurrent calls, partial prefix matches, multiple breakpoints and
  cross-region routing are not modeled.
- Savings are incremental to the cache reads and writes already observed, which are all
  attributed to the fingerprinted prefix (a conservative choice); a prefix below the
  model's catalog minimum gets no scenario; group results are not additive and are not
  summed anywhere.
- Prefix token counts supplied by the application as estimates (method `estimated_*`) are
  evidence class D; scenario amounts built on them inherit that caveat.
- Monthly projections exist only when `--monthly-requests` is passed and are labeled as
  projected scenarios.

## Findings

- Savings are never summed across findings; only the best single action is reported and
  joint savings are `not computed`.
- A failed run's cost is reported without savings; a used fallback is never a finding; an
  unlabeled fallback yields no savings.
- The detectors read application labels; they never infer dispositions from model output.

## Collection

- LiteLLM router retries and fallbacks are disabled in the example config; if a gateway
  retries silently outside the deployment hooks, those attempts are not captured and the
  coverage is `unsupported`, not complete.
- Streaming attempts get their terminal from LiteLLM's request-level success/failure log
  events (the deployment success hook does not fire for streams); a stream the client
  abandons before the final chunk stays `in_flight` with unknown cost.
- The collector keeps up to an hour of attempt state in memory per process; lineage across
  processes or after restarts is not reconstructed.
- HMAC fingerprints reduce content exposure; they are not an anonymization guarantee.

## Tooling

- Python 3.12 only; CI covers Linux. Windows and macOS are used ad hoc.
- The HTML report was checked structurally (self-contained, no external resources,
  responsive CSS); a manual check in several browsers is still recommended before
  publishing a real report.
- A report refuses to render when its runs are stale; `--allow-stale` renders with a
  banner but the numbers then describe an older state of the workspace.
- The spend fuse is a local budget control based on known prices; it does not replace
  provider-side spend limits.

## Non-goals in v0.1

SPA or hosted dashboard, FastAPI service, custom gateway, login or multi-tenant service,
ClickHouse / Kafka / Kubernetes, OTLP collector, generic tracing UI, automatic routing or
policy enforcement, an LLM in the analysis path, a full economics graph, image / audio /
video / Batch API / server-side tool billing, self-hosted GPU cost, vLLM / SGLang adapters.
