"""Findings and Context Economics groups (PLAN.md §8.1, §9.1)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from aiecon.spec.common import (
    CodeStr,
    DataKind,
    DecimalStr,
    IdStr,
    NonNegInt,
    Provider,
    ShortText,
    SpecModel,
    UtcMs,
)


class EvidenceLevel(StrEnum):
    observed = "observed"
    labeled = "labeled"
    modeled = "modeled"
    insufficient = "insufficient"


class ConfidenceClass(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class DetectorName(StrEnum):
    discarded_attempt = "retry_waste.discarded_attempt"
    failed_run = "retry_waste.failed_run"
    repeated_context = "repeated_context"
    unused_fallback = "fallback_waste.unused_fallback"


class Finding(SpecModel):
    finding_id: IdStr
    detector: DetectorName
    detector_version: str = Field(max_length=40)
    dataset_id: IdStr
    data_kind: DataKind
    scope_id: IdStr
    evidence_event_ids: list[IdStr] = Field(default_factory=list)
    call_ids: list[IdStr] = Field(default_factory=list)
    line_item_ids: list[IdStr] = Field(default_factory=list)
    affected_run_ids: list[IdStr] = Field(default_factory=list)
    observed_cost_usd: DecimalStr | None = None
    observed_cost_complete: bool | None = None
    unknown_cost_call_count: NonNegInt = 0
    modeled_savings_usd: DecimalStr | None = None
    confidence_class: ConfidenceClass
    evidence_level: EvidenceLevel
    proposed_action: ShortText
    assumptions: list[ShortText] = Field(default_factory=list)
    caveats: list[ShortText] = Field(default_factory=list)
    summary: ShortText
    scenario: dict[str, str | int | None] | None = Field(
        default=None, description="Scenario parameters; decimals rendered as strings"
    )

    @model_validator(mode="after")
    def _savings_need_evidence(self) -> Finding:
        if self.modeled_savings_usd is not None and (
            self.evidence_level is EvidenceLevel.insufficient
        ):
            raise ValueError("insufficient evidence cannot carry modeled savings")
        return self


class ContextRecommendation(StrEnum):
    beneficial = "beneficial"
    not_beneficial = "not_beneficial"
    insufficient_evidence = "insufficient_evidence"
    already_cached = "already_cached"


class CacheSegment(SpecModel):
    """One cold-start segment: a write followed by reads within the policy TTL."""

    first_call_id: IdStr
    call_count: NonNegInt
    started_at_ms: UtcMs
    ended_at_ms: UtcMs


class ContextEconGroup(SpecModel):
    group_id: IdStr
    dataset_id: IdStr
    data_kind: DataKind
    scope_id: IdStr
    provider: Provider
    model_resolved: IdStr
    cache_policy: CodeStr | None = None
    fingerprint_key_id: IdStr | None = None
    prefix_fingerprint: str = Field(max_length=128)
    calls: NonNegInt
    call_ids: list[IdStr] = Field(default_factory=list)
    prefix_tokens: NonNegInt | None = None
    prefix_token_count_method: CodeStr | None = None
    first_seen_ms: UtcMs
    last_seen_ms: UtcMs
    reuse_intervals_ms: list[NonNegInt] = Field(default_factory=list)
    observed_cache_read_tokens: NonNegInt | None = None
    observed_cache_write_tokens: NonNegInt | None = None
    candidate_repeated_prefix_tokens: NonNegInt | None = None
    scenario_policy: CodeStr | None = None
    segments: list[CacheSegment] = Field(default_factory=list)
    modeled_no_cache_cost_usd: DecimalStr | None = None
    modeled_baseline_cost_usd: DecimalStr | None = Field(
        default=None,
        description=(
            "What the repeated prefix costs today given the observed cache reads and writes; "
            "modeled_savings_usd is measured against this, not against no cache at all"
        ),
    )
    modeled_cache_cost_usd: DecimalStr | None = None
    modeled_savings_usd: DecimalStr | None = None
    break_even_reuses: NonNegInt | None = None
    supported_ttl_candidates: list[CodeStr] = Field(default_factory=list)
    recommendation: ContextRecommendation
    evidence_level: EvidenceLevel
    confidence: ConfidenceClass
    assumptions: list[ShortText] = Field(default_factory=list)
    caveats: list[ShortText] = Field(default_factory=list)
