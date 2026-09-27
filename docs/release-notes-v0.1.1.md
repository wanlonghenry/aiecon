# aiecon v0.1.1 — correctness pass

A correctness release driven by an external gap review of v0.1.0 (items G01–G10). No new
features; several numbers now say less, on purpose. Offline only: live reconciliation is
still pending for both providers.

## Fixed

- **Streaming calls never got a terminal event.** The LiteLLM deployment success hook does
  not fire for streams, so streamed attempts stayed `in_flight` forever. The request-level
  success/failure log events are now the terminal source; the first terminal wins and
  repeats are counted, not written. Reproduced against the real router before the fix.
- **Snapshots could double-count or lose days.** Every provider pull is kept; for each
  provider, scope, record kind and UTC day the latest complete fetch covering that day is the
  one compared. A one-day re-pull no longer deactivates the other days of an earlier pull.
  Idempotency is per snapshot id; the same id with different bytes is refused.
- **Capture gaps were priced with another model's rates.** The provider-side surplus is now
  priced per model with that model's own line-item rates and per cache-write tier. It counts
  as evidence only when every snapshot feeding the day declares the scope dedicated to the
  captured traffic (`billing sync --dedicated-scope`, `scope_dedicated` in manifests);
  otherwise it is a hypothesis because other traffic looks identical from the provider side.
- **Incomparable data still produced strong conclusions.** A day whose estimate is a
  known-cost lower bound is never `matched` by tolerance (it is `unpriced` unless E equals
  B); daily provider grains are compared over whole UTC days only; the time-based
  "provisional" guess is gone; the shareable one-liner names lower bounds and provisional data.
- **Context-economics savings were overstated.** Savings are now incremental to the cache
  reads and writes already observed, a prefix below the model's minimum cacheable length gets
  no scenario, and every group states that results are not additive.
- **Reports could mix old runs with new data.** `aiecon report` refuses to render when the
  active pricing run no longer matches the calls, or the reconcile run no longer matches the
  pricing run or the snapshots on file. `--allow-stale` renders with a STALE INPUTS banner.
- **Input hashes collapsed same-named files.** They are keyed by workspace-relative paths
  and limited to the report's dataset.

## Changed

- `ProviderSnapshotManifest.scope_dedicated`, `ContextEconGroup.modeled_baseline_cost_usd`,
  `ReportIdentity.stale_inputs`, `CallFinishedPayload.provider_created_at_ms` added (all
  optional; schema version unchanged).
- `ImportResult.superseded_snapshot_ids` replaces `deactivated_snapshot_ids`; the CLI prints
  `superseded_snapshots`.
- LiteLLM adapter: an absent or null cache-read counter is read as zero (LiteLLM contract);
  the note `cache_read_absent_treated_as_zero` marks such calls.
- CI gains a job that installs the `live` extra and drives the real LiteLLM router callbacks
  with mock responses; still no network and no keys.

## Verification

- 140 offline tests (v0.1.0: 128), demo 313 calls / 100 outcomes, acceptance checks 26/26.
- Docs: README, `docs/integration.md` (`uv run --env-file .env`, one scope id everywhere),
  `docs/architecture.md`, `KNOWN_LIMITATIONS.md`, PLAN.md section 19 restructured into
  implementation / offline verification / live verification / evidence / open gaps.
