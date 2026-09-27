"""Reconciliation buckets (PLAN.md §7.5–§7.6)."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import Field, model_validator

from aiecon.spec.common import (
    CodeStr,
    DataKind,
    DecimalStr,
    IdStr,
    IntMap,
    NonNegInt,
    Provider,
    ShortText,
    SpecModel,
    UtcMs,
)
from aiecon.spec.provider import Finality, RecordKind


class ComparisonKind(StrEnum):
    usage_comparison = "usage_comparison"
    cost_comparison = "cost_comparison"


class ReconciliationStatus(StrEnum):
    matched = "matched"
    variance = "variance"
    provisional = "provisional"
    scope_mismatch = "scope_mismatch"
    unpriced = "unpriced"
    no_provider_usage = "no_provider_usage"
    no_provider_cost = "no_provider_cost"


KNOWN_DIAGNOSIS_CODES: tuple[str, ...] = (
    "capture_gap",
    "price_version",
    "cache_tier",
    "scope_mismatch",
    "late_data",
    "unsupported_charge",
    "boundary_call",
    "unpriced_calls",
    "unattributed_calls",
    "rounding",
)


class Adjustment(SpecModel):
    """A signed explanation of part of the variance. Only evidence-backed ones count."""

    code: CodeStr
    signed_amount_usd: DecimalStr
    evidence_backed: bool
    evidence_refs: list[IdStr] = Field(
        default_factory=list, description="call ids, line item ids, record ids or snapshot ids"
    )
    description: ShortText


class ReconciliationBucket(SpecModel):
    reconcile_run_id: IdStr
    bucket_key: IdStr
    comparison_kind: ComparisonKind
    dataset_id: IdStr
    data_kind: DataKind
    provider: Provider
    scope_id: IdStr
    window_start_ms: UtcMs
    window_end_ms: UtcMs
    grain: str = Field(max_length=200)
    dimensions: dict[str, str | None] = Field(default_factory=dict)
    record_kind: RecordKind | None = None
    finality: Finality = Finality.unknown

    # local side
    pricing_run_id: IdStr | None = None
    local_call_count: NonNegInt = 0
    local_unpriced_call_count: NonNegInt = 0
    local_unattributed_call_count: NonNegInt = 0
    local_usage: IntMap | None = None
    local_estimate_usd: DecimalStr | None = None
    local_cost_complete: bool | None = None

    # provider side
    provider_snapshot_ids: list[IdStr] = Field(default_factory=list)
    provider_record_ids: list[IdStr] = Field(default_factory=list)
    provider_usage: IntMap | None = None
    provider_cost_usd: DecimalStr | None = None

    # comparison
    signed_variance_usd: DecimalStr | None = None
    absolute_variance_usd: DecimalStr | None = None
    variance_pct: DecimalStr | None = Field(
        default=None, description="(E - B) / B * 100; null unless B > 0"
    )
    usage_variance: dict[str, int] | None = Field(
        default=None, description="local - provider per metric; null metrics omitted"
    )
    tolerance_usd: DecimalStr | None = None
    status: ReconciliationStatus
    reasons: list[CodeStr] = Field(default_factory=list)
    explained_adjustments: list[Adjustment] = Field(default_factory=list)
    hypotheses: list[Adjustment] = Field(default_factory=list)
    unexplained_delta_usd: DecimalStr | None = None
    unmodeled_charges_usd: DecimalStr | None = Field(
        default=None,
        description="Provider charges outside modeled categories, listed separately",
    )

    @model_validator(mode="after")
    def _algebra(self) -> ReconciliationBucket:
        if self.window_end_ms <= self.window_start_ms:
            raise ValueError("window_end_ms must be greater than window_start_ms")
        for adj in self.explained_adjustments:
            if not adj.evidence_backed:
                raise ValueError("explained adjustments must be evidence-backed")
        for adj in self.hypotheses:
            if adj.evidence_backed:
                raise ValueError("hypotheses cannot be marked evidence-backed")
        if self.signed_variance_usd is not None and self.unexplained_delta_usd is not None:
            explained = sum(
                (a.signed_amount_usd for a in self.explained_adjustments), start=Decimal(0)
            )
            if self.signed_variance_usd - explained != self.unexplained_delta_usd:
                raise ValueError("unexplained_delta must equal signed_variance - explained")
        return self
