"""ModelCall: the current projection of one physical provider attempt (PLAN.md §4.3)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from aiecon.spec.common import (
    ApiFamily,
    CodeStr,
    DataKind,
    DecimalStr,
    HexStr,
    IdStr,
    IntMap,
    NonNegInt,
    PosInt,
    Provider,
    SafeUsage,
    SpecModel,
    UtcMs,
    VersionStr,
)
from aiecon.spec.envelope import CallStatus, UsageFormat


class UsageCompleteness(StrEnum):
    complete = "complete"
    partial = "partial"
    missing = "missing"
    invalid = "invalid"


class ModelCall(SpecModel):
    # identity
    dataset_id: IdStr
    call_id: IdStr
    data_kind: DataKind
    scope_id: IdStr
    workflow_id: IdStr | None = None
    workflow_run_id: IdStr | None = None
    node_id: IdStr | None = None
    node_run_id: IdStr | None = None
    attempt_index: PosInt | None = None
    retry_of_call_id: IdStr | None = None
    fallback_of_call_id: IdStr | None = None
    trace_id: IdStr | None = None
    span_id: IdStr | None = None

    # provenance
    source_event_ids: list[IdStr] = Field(default_factory=list)
    source_content_hashes: list[str] = Field(default_factory=list)
    normalizer_version: VersionStr
    revision: PosInt = 1

    # call
    provider: Provider
    api_family: ApiFamily = ApiFamily.unknown
    model_requested: IdStr
    model_resolved: IdStr | None = None
    service_tier: CodeStr | None = None
    inference_region: CodeStr | None = None
    provider_request_id: IdStr | None = None
    stream: bool | None = None

    # time & status
    started_at_ms: UtcMs | None = None
    ended_at_ms: UtcMs | None = None
    status: CallStatus
    error_class: CodeStr | None = None

    # usage (mutually exclusive input classes; None = unknown, never 0 by default)
    usage_format: UsageFormat | None = None
    input_total_tokens: NonNegInt | None = None
    input_uncached_tokens: NonNegInt | None = None
    input_cache_read_tokens: NonNegInt | None = None
    input_cache_write_tokens: NonNegInt | None = None
    output_tokens: NonNegInt | None = None
    cache_write_breakdown: IntMap | None = Field(
        default=None,
        description="Mutually exclusive provider write tiers, e.g. ephemeral_5m / ephemeral_1h",
    )
    usage_completeness: UsageCompleteness = UsageCompleteness.missing
    usage_notes: list[CodeStr] = Field(
        default_factory=list, description="Normalizer notes, e.g. cache_read_field_absent"
    )
    provider_usage_safe: SafeUsage | None = None
    upstream_cost_estimate_usd: DecimalStr | None = None
    schema_drift: IntMap | None = None

    # context economics
    prefix_fingerprint: HexStr | None = None
    fingerprint_key_id: IdStr | None = None
    prefix_tokens: NonNegInt | None = None
    prefix_token_count_method: CodeStr | None = None
    cache_policy: CodeStr | None = None

    @model_validator(mode="after")
    def _input_identity(self) -> ModelCall:
        parts = (
            self.input_uncached_tokens,
            self.input_cache_read_tokens,
            self.input_cache_write_tokens,
        )
        if self.input_total_tokens is not None and all(p is not None for p in parts):
            if sum(p for p in parts if p is not None) != self.input_total_tokens:
                raise ValueError(
                    "input_total_tokens must equal uncached + cache_read + cache_write"
                )
        if self.cache_write_breakdown and self.input_cache_write_tokens is not None:
            if sum(self.cache_write_breakdown.values()) != self.input_cache_write_tokens:
                raise ValueError("cache_write_breakdown must sum to input_cache_write_tokens")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.status is not CallStatus.in_flight

    @property
    def is_attributed(self) -> bool:
        return self.workflow_run_id is not None and self.node_id is not None

    @property
    def price_selection_ts_ms(self) -> int | None:
        """Timestamp used to pick a price version: started_at, else ended_at."""

        if self.started_at_ms is not None:
            return self.started_at_ms
        return self.ended_at_ms
