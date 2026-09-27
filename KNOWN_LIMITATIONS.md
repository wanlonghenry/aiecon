# Known limitations (v0.1.0)

Honest boundaries of this release. Items marked *pending* are planned; items marked
*non-goal* are deliberately outside v0.1 (PLAN.md sections 1.3 and 17).

## Validation

- **No live reconciliation has been performed yet.** Both providers are at L0
  (fixture-verified parsing, pricing, import, reconciliation, report). L1 — comparing real
  calls with real provider cost reports over the same scope and window — is pending for
  OpenAI and for Anthropic because no admin keys were available during the build. The
  README and the report say `live reconciliation pending`; do not read "matched" in the
  synthetic demo as a real-world accuracy claim.
- The `litellm_standard` usage fixtures were written from LiteLLM 1.102.1 documentation,
  not captured from a live run; the exact transformed layout should be captured on the
  first live call.
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
- Anthropic cache writes without a 5m/1h breakdown remain unpriced; OpenAI cache writes
  without a counter make a write-billed model's estimate a lower bound.
- Calls with missing or invalid usage (timeouts, interrupted streams, failed primaries)
  have unknown cost. Totals that include them are labeled known-cost lower bounds.
- Reasoning tokens are priced inside output tokens; no separate reasoning price exists.
- Service tiers other than `standard` and regions other than `global` are not priced.
  Anthropic US-only inference (1.1x) and OpenAI fast/flex/batch tiers are unpriced.

## Context economics

- Scenarios assume sequential calls, one write per cold-start segment, no eviction, and a
  prefix that is repeated verbatim; TTLs are fixed per policy (5 min, 60 min, and a
  conservative 5 min for OpenAI automatic caching).
- Overlapping concurrent calls, partial prefix matches, multiple breakpoints and
  cross-region routing are not modeled.
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
- The collector keeps up to an hour of attempt state in memory per process; lineage across
  processes or after restarts is not reconstructed.
- HMAC fingerprints reduce content exposure; they are not an anonymization guarantee.

## Tooling

- Python 3.12 only; CI covers Linux. Windows and macOS are used ad hoc.
- The HTML report was checked structurally (self-contained, no external resources,
  responsive CSS); a manual check in several browsers is still recommended before
  publishing a real report.
- The spend fuse is a local budget control based on known prices; it does not replace
  provider-side spend limits.

## Non-goals in v0.1

SPA or hosted dashboard, FastAPI service, custom gateway, login or multi-tenant service,
ClickHouse / Kafka / Kubernetes, OTLP collector, generic tracing UI, automatic routing or
policy enforcement, an LLM in the analysis path, a full economics graph, image / audio /
video / Batch API / server-side tool billing, self-hosted GPU cost, vLLM / SGLang adapters.
