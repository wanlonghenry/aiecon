# aiecon Agent Instructions

Read PLAN.md before changing the implementation.

Project: one fully public repository, aiecon.
All v0.1 code, detectors, context economics, templates, fixtures, and CLI
belong in this repository. There is no private server dependency.

Build the smallest local pipeline that reconstructs call costs,
compares usage and monetary records at supported source grains,
and produces reproducible evidence-backed findings.

For each task:
1. State the task ID, inputs, outputs, files, and acceptance criteria.
2. Check dependencies and current implementation before editing.
3. Write meaningful tests first for pricing, reconciliation, and deduplication.
4. Implement only the selected task and necessary dependencies.
5. Run the relevant verification and report what it proves.
6. Update PLAN.md and commit the logical change.

Core rules:
- Use Python 3.12, uv, Pydantic, DuckDB, Typer, and Jinja2.
- Core imports and offline demo must not require provider credentials.
- Use Decimal for money and explicit units for quantities.
- Preserve append-only, allowlisted telemetry before normalization.
- Never persist raw prompt/response/tool content or arbitrary metadata.
- Ingestion and estimation must be replayable and idempotent.
- One physical provider attempt is one call; retain retry/fallback lineage.
- Missing usage or unknown prices are unknown, never silently zero.
- Keep provider usage, provider cost, and settled cost distinct.
- Reconcile only comparable scopes and supported dimensions.
- Never overwrite estimates to make totals match.
- Never upgrade call-level estimates to billed amounts after bucket matching.
- Findings need evidence and assumptions; joint savings are not implemented.
- Keep synthetic data visibly synthetic in every public example.
- Do not put an LLM in the analysis path.
- Verify mutable provider contracts in current official documentation.
- Record the tested dependency versions and known limitations.

Scope exclusions:
No SaaS, auth, web service, custom gateway, dashboard framework,
ClickHouse/Kafka/Kubernetes, OTLP collector, full graph engine,
automatic routing/enforcement, multimodal billing, Batch execution,
or live GPU costing in v0.1.

Workflow:
Use one task at a time in dependency order. Parallel agents are optional
only when the user explicitly requests them; they are not a prerequisite.
Resolve routine implementation choices using this plan.
Do not add approval steps for reversible local edits or ordinary tests.
Live workload defaults to dry-run and requires explicit spend flags.
Publish only synthetic example data from the repository.

Finish the acceptance criteria before proceeding to adjacent features.
If blocked on provider credentials or delayed billing, continue the
offline/import path and record the precise live validation limitation.
