"""Report data model (PLAN.md §14): JSON is the interface, HTML renders the same object."""

from __future__ import annotations

from pydantic import Field

from aiecon.spec.common import (
    CodeStr,
    DataKind,
    DecimalStr,
    IdStr,
    NonNegInt,
    ShortText,
    SpecModel,
    TimeWindow,
    UtcMs,
    VersionStr,
)
from aiecon.spec.finding import ContextEconGroup, Finding
from aiecon.spec.reconcile import ReconciliationBucket


class ReportIdentity(SpecModel):
    dataset_id: IdStr
    data_kind: DataKind
    workspace_id: IdStr | None = None
    generated_at_ms: UtcMs
    window: TimeWindow | None = None
    aiecon_version: VersionStr
    schema_version: VersionStr
    normalizer_version: VersionStr | None = None
    pricing_run_id: IdStr | None = None
    catalog_version: VersionStr | None = None
    catalog_kind: str | None = None
    reconcile_run_id: IdStr | None = None
    provider_snapshot_ids: list[IdStr] = Field(default_factory=list)
    data_sources: list[CodeStr] = Field(default_factory=list)
    finality_counts: dict[str, int] = Field(default_factory=dict)
    call_count: NonNegInt = 0
    outcome_count: NonNegInt = 0


class OutcomeSection(SpecModel):
    cohort_definition: ShortText
    cohort_runs: NonNegInt
    succeeded: NonNegInt
    failed: NonNegInt
    abandoned: NonNegInt
    pending_or_unknown_runs: NonNegInt
    runs_without_outcome: NonNegInt
    unattributed_calls: NonNegInt
    cohort_call_count: NonNegInt
    cohort_known_cost_usd: DecimalStr
    unknown_cost_calls: NonNegInt
    cost_per_successful_outcome_usd: DecimalStr | None
    successful_runs_only_cost_usd: DecimalStr
    successful_runs_only_per_success_usd: DecimalStr | None
    is_lower_bound: bool
    note: ShortText


class CostGroup(SpecModel):
    workflow_id: str | None
    node_id: str | None
    provider: str
    model: str
    calls: NonNegInt
    known_cost_usd: DecimalStr
    unpriced_calls: NonNegInt
    input_total_tokens: NonNegInt
    output_tokens: NonNegInt


class ReconciliationSection(SpecModel):
    ran: bool
    note: ShortText | None = None
    buckets: list[ReconciliationBucket] = Field(default_factory=list)
    summary_lines: list[str] = Field(default_factory=list)
    total_local_estimate_usd: DecimalStr | None = None
    total_provider_cost_usd: DecimalStr | None = None
    total_unmodeled_charges_usd: DecimalStr | None = None
    status_counts: dict[str, int] = Field(default_factory=dict)


class UsageCoverageSection(SpecModel):
    buckets: list[ReconciliationBucket] = Field(default_factory=list)
    local_usage: dict[str, int] = Field(default_factory=dict)
    provider_usage: dict[str, int] = Field(default_factory=dict)
    calls_missing_usage: NonNegInt = 0
    calls_unpriced: NonNegInt = 0
    unpriced_reasons: dict[str, int] = Field(default_factory=dict)
    unmatched_scopes: list[str] = Field(default_factory=list)


class WasteSection(SpecModel):
    findings: list[Finding] = Field(default_factory=list)
    by_detector: dict[str, int] = Field(default_factory=dict)


class MonthlyProjection(SpecModel):
    requests_per_month: NonNegInt
    observed_calls_in_window: NonNegInt
    scale_factor: DecimalStr
    projected_savings_usd: DecimalStr
    group_id: IdStr
    assumptions: list[ShortText]


class ContextSection(SpecModel):
    groups: list[ContextEconGroup] = Field(default_factory=list)
    calls_with_prefix_evidence: NonNegInt = 0
    calls_without_prefix_evidence: NonNegInt = 0
    projections: list[MonthlyProjection] = Field(default_factory=list)
    note: ShortText | None = None


class OpportunitiesSection(SpecModel):
    unique_flagged_cost_usd: DecimalStr
    unique_flagged_line_items: NonNegInt
    unique_flagged_calls: NonNegInt
    unknown_cost_flagged_calls: NonNegInt
    best_single_action_savings_usd: DecimalStr | None
    best_single_action_finding_id: IdStr | None
    joint_savings: ShortText


class LimitationsSection(SpecModel):
    validation_status: list[ShortText] = Field(default_factory=list)
    unpriced_reasons: dict[str, int] = Field(default_factory=dict)
    unsupported_charges_usd: DecimalStr | None = None
    provisional_snapshots: NonNegInt = 0
    input_file_hashes: dict[str, str] = Field(default_factory=dict)
    price_sources: list[str] = Field(default_factory=list)
    notes: list[ShortText] = Field(default_factory=list)


class Report(SpecModel):
    identity: ReportIdentity
    outcome_economics: OutcomeSection
    cost_breakdown: list[CostGroup]
    monetary_reconciliation: ReconciliationSection
    usage_coverage: UsageCoverageSection
    waste_findings: WasteSection
    context_economics: ContextSection
    opportunities: OpportunitiesSection
    limitations: LimitationsSection


class ReportManifest(SpecModel):
    report_id: IdStr
    dataset_id: IdStr
    data_kind: DataKind
    generated_at_ms: UtcMs
    git_commit: str | None = None
    aiecon_version: VersionStr
    schema_version: VersionStr
    normalizer_version: VersionStr | None = None
    catalog_version: VersionStr | None = None
    catalog_hash: str | None = None
    pricing_run_id: IdStr | None = None
    reconcile_run_id: IdStr | None = None
    provider_snapshot_ids: list[IdStr] = Field(default_factory=list)
    window: TimeWindow | None = None
    input_file_hashes: dict[str, str] = Field(default_factory=dict)
    report_json_sha256: str
    report_html_sha256: str
