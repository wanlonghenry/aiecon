"""Provider usage / cost records and snapshot manifests (PLAN.md §7.1–§7.4)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from aiecon.spec.common import (
    SCHEMA_VERSION,
    CodeStr,
    CurrencyStr,
    DataKind,
    DecimalStr,
    IdStr,
    IntMap,
    NonNegInt,
    Provider,
    ShortText,
    SpecModel,
    TimeWindow,
    UtcMs,
)


class RecordKind(StrEnum):
    provider_usage = "provider_usage"
    provider_cost = "provider_cost"
    settled_cost = "settled_cost"


class Finality(StrEnum):
    provisional = "provisional"
    settled = "settled"
    unknown = "unknown"


class BucketWidth(StrEnum):
    minute = "1m"
    hour = "1h"
    day = "1d"
    none = "none"


NOT_GROUPED = "not_grouped"
"""Sentinel dimension value: the source did not group by this dimension (§7.2)."""


def grain_key(bucket_width: BucketWidth | str, dimensions: list[str] | tuple[str, ...]) -> str:
    """Canonical grain label, e.g. ``1d/model,project_id``. Dimension order is sorted."""

    width = bucket_width.value if isinstance(bucket_width, BucketWidth) else str(bucket_width)
    dims = ",".join(sorted(set(dimensions)))
    return f"{width}/{dims}"


class ProviderRecord(SpecModel):
    snapshot_id: IdStr
    record_id: IdStr
    provider: Provider
    scope_id: IdStr
    record_kind: RecordKind
    data_kind: DataKind
    window_start_ms: UtcMs
    window_end_ms: UtcMs
    grain: str = Field(max_length=200, description="Canonical grain label from grain_key()")
    dimensions: dict[str, str | None] = Field(
        default_factory=dict,
        description=(
            "Source dimensions. Absent key = not applicable; None = provider returned null "
            "(e.g. default workspace); 'not_grouped' = not requested in grouping."
        ),
    )
    amount_original: DecimalStr | None = None
    amount_unit: CodeStr | None = Field(
        default=None, description="Unit of amount_original, e.g. 'usd' or 'cents'"
    )
    currency: CurrencyStr | None = None
    amount_usd: DecimalStr | None = None
    usage: IntMap | None = Field(
        default=None,
        description=(
            "Normalized mutually exclusive metrics: input_uncached_tokens, "
            "input_cache_read_tokens, input_cache_write_tokens, input_total_tokens, "
            "output_tokens, requests"
        ),
    )
    usage_native: IntMap | None = Field(
        default=None, description="Provider-native metric names as returned, for evidence"
    )
    source_ref: ShortText
    source_hash: str = Field(max_length=128)
    fetched_at_ms: UtcMs
    finality: Finality = Finality.unknown
    snapshot_complete: bool

    @model_validator(mode="after")
    def _window_and_kind(self) -> ProviderRecord:
        if self.window_end_ms <= self.window_start_ms:
            raise ValueError("window_end_ms must be greater than window_start_ms")
        if self.record_kind is RecordKind.provider_usage:
            if self.amount_usd is not None or self.amount_original is not None:
                raise ValueError("provider_usage records carry usage, not amounts")
        elif self.amount_original is None and self.amount_usd is None:
            raise ValueError("cost records must carry an amount")
        if self.amount_original is not None and self.amount_unit is None:
            raise ValueError("amount_unit is required when amount_original is present")
        return self


class ProviderSnapshotManifest(SpecModel):
    schema_version: str = SCHEMA_VERSION
    snapshot_id: IdStr
    provider: Provider
    scope_id: IdStr
    record_kind: RecordKind
    data_kind: DataKind
    grain: str = Field(max_length=200)
    query_window: TimeWindow
    fetched_at_ms: UtcMs
    source_ref: ShortText
    source_hash: str = Field(max_length=128)
    finality: Finality
    snapshot_complete: bool
    record_count: NonNegInt | None = None
    currency: CurrencyStr | None = None
    notes: ShortText | None = None
    source_type: CodeStr = Field(default="file_import", description="file_import | api_poller")
    scope_filter: dict[str, str] | None = Field(
        default=None, description="Provider-side filter used (project/workspace ids)"
    )
    scope_dedicated: bool | None = Field(
        default=None,
        description=(
            "True when the provider scope carries only the traffic captured locally, so a "
            "provider-side surplus is evidence of a capture gap rather than other traffic; "
            "None means unknown"
        ),
    )
