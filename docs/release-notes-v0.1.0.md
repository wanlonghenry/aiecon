First public prototype: a local, reproducible pipeline that reconstructs LLM call costs, prices them from a versioned catalog, reconciles usage and money with provider reports at the provider's real grain, and produces evidence-backed findings in a single-page report.

## What works (L0 — reproducible synthetic demo)

- `git clone → uv sync --locked → uv run aiecon demo` produces `report.html`, `report.json`, `report-manifest.json` and a rebuildable DuckDB file with no API key and no network.
- Allowlisted, append-only JSONL telemetry; idempotent replay (same event twice never doubles a call; same event id with different content is a conflict, exit code 3).
- Usage normalization for `openai_chat_completions`, `openai_responses` (including `cache_write_tokens`), `anthropic_messages` (5m/1h cache tiers) and LiteLLM-transformed usage; streaming final-usage rules for both providers.
- Versioned price catalogs with effective dates and explicit aliases; a pure estimator that leaves anything it cannot price *unpriced with a reason* instead of guessing.
- Provider usage/cost snapshots by normalized CSV/JSON import (with manifest hash) or read-only Admin API pollers (pagination, bounded retries, staging before activation); Anthropic cent amounts converted exactly.
- Reconciliation per provider, scope and UTC day (plus per model where the provider reports it): usage and money compared separately, evidence-backed adjustments, the remainder kept as `unexplained`, tolerance only classifies.
- Detectors for discarded attempts, failed runs, unused fallbacks and repeated context; a union summary that never double counts and reports only the best single action.
- Context Economics v1: prefix groups, observed provider cache usage, TTL segments and the section 8.3 scenario formula, with `not_beneficial`, `already_cached` and `insufficient_evidence` as first-class outcomes.
- Per-attempt LiteLLM 1.102.1 collector with explicit retry/fallback lineage, a spend fuse and an explicit live workload script that defaults to dry run.

## What is not validated yet (L1 pending)

- No real provider cost report has been reconciled yet for OpenAI or Anthropic: no admin keys were available during the build. The README and every report say `live reconciliation pending`. The synthetic demo proves the computation path, not real-world accuracy.
- The LiteLLM-transformed usage fixtures were written from documentation, not captured from a live run.

See `KNOWN_LIMITATIONS.md` and `docs/provider-assumptions.md` for the full list, including unverified provider behaviours.

## Verification in this release

- 128 tests (unit, integration, offline e2e), Ruff clean, `uv build` wheel installed into a fresh virtual environment and the demo run from an unrelated directory with a dead proxy.
- CI runs the same steps on Linux plus a full-history secret scan; it never holds provider credentials or runs paid calls.

License: Apache-2.0.
