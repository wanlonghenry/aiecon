"""Price catalog, cost line items and pricing-run manifest (PLAN.md §6)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from aiecon.spec.common import (
    ApiFamily,
    CodeStr,
    CurrencyStr,
    DataKind,
    DecimalStr,
    EvidenceClass,
    IdStr,
    NonNegInt,
    PosInt,
    Provider,
    ShortText,
    SpecModel,
    UtcMs,
    VersionStr,
    canonical_dumps,
    sha256_hex,
)


class Resource(StrEnum):
    """Mutually exclusive priced resources. One token belongs to exactly one class."""

    input_uncached = "input_uncached"
    input_cache_read = "input_cache_read"
    input_cache_write = "input_cache_write"  # single-tier write price
    input_cache_write_5m = "input_cache_write_5m"
    input_cache_write_1h = "input_cache_write_1h"
    output = "output"
    request = "request"


INPUT_RESOURCES: tuple[Resource, ...] = (
    Resource.input_uncached,
    Resource.input_cache_read,
    Resource.input_cache_write,
    Resource.input_cache_write_5m,
    Resource.input_cache_write_1h,
)

CACHE_WRITE_RESOURCES: tuple[Resource, ...] = (
    Resource.input_cache_write,
    Resource.input_cache_write_5m,
    Resource.input_cache_write_1h,
)


class PriceUnit(StrEnum):
    token = "token"
    request = "request"


class EffectiveDateBasis(StrEnum):
    provider_announced = "provider_announced"
    observed_at_retrieval = "observed_at_retrieval"
    synthetic = "synthetic"


class PriceRecord(SpecModel):
    price_id: IdStr
    catalog_version: VersionStr
    provider: Provider
    model_id: IdStr
    api_family: ApiFamily | None = Field(default=None, description="None applies to every family")
    resource: Resource
    service_tier: CodeStr = "standard"
    region: CodeStr = "global"
    context_band: CodeStr = "default"
    effective_from_ms: UtcMs
    effective_to_ms: UtcMs | None = None
    currency: CurrencyStr = "USD"
    unit: PriceUnit
    unit_quantity: PosInt = Field(
        default=1_000_000, description="Price applies per this many units"
    )
    unit_price: DecimalStr
    source_url: str = Field(max_length=500)
    retrieved_at: str = Field(max_length=40, description="ISO-8601 timestamp of retrieval")
    effective_date_basis: EffectiveDateBasis
    notes: ShortText | None = None

    @model_validator(mode="after")
    def _window(self) -> PriceRecord:
        if self.effective_to_ms is not None and self.effective_to_ms <= self.effective_from_ms:
            raise ValueError("effective_to_ms must be after effective_from_ms")
        if self.unit_price < 0:
            raise ValueError("unit_price cannot be negative")
        return self

    def is_effective_at(self, ts_ms: int) -> bool:
        if ts_ms < self.effective_from_ms:
            return False
        return self.effective_to_ms is None or ts_ms < self.effective_to_ms


class ModelAlias(SpecModel):
    provider: Provider
    alias: IdStr
    model_id: IdStr
    source_url: str = Field(max_length=500)
    retrieved_at: str = Field(max_length=40)
    notes: ShortText | None = None


class ModelCacheContract(SpecModel):
    """What the catalog knows about a model's cache billing shape (§6.3)."""

    provider: Provider
    model_id: IdStr
    cache_write_tiers: list[Resource] = Field(
        default_factory=list,
        description="Write resources that exist for this model (empty = writes are not billed)",
    )
    supported_cache_policies: list[CodeStr] = Field(default_factory=list)
    min_cacheable_prefix_tokens: NonNegInt | None = None
    source_url: str = Field(max_length=500)
    retrieved_at: str = Field(max_length=40)
    notes: ShortText | None = None


class PriceCatalog(SpecModel):
    catalog_version: VersionStr
    catalog_kind: Literal["synthetic", "real"]
    description: ShortText
    currency: CurrencyStr = "USD"
    records: list[PriceRecord]
    aliases: list[ModelAlias] = Field(default_factory=list)
    cache_contracts: list[ModelCacheContract] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> PriceCatalog:
        seen: set[str] = set()
        for record in self.records:
            if record.price_id in seen:
                raise ValueError("duplicate price_id in catalog")
            seen.add(record.price_id)
            if record.catalog_version != self.catalog_version:
                raise ValueError("price record catalog_version differs from catalog")
            if self.catalog_kind == "synthetic" and not record.model_id.startswith("fixture-"):
                raise ValueError("synthetic catalogs may only price fixture-* models")
            if self.catalog_kind == "real" and (
                record.effective_date_basis is EffectiveDateBasis.synthetic
            ):
                raise ValueError("real catalogs cannot use synthetic effective_date_basis")
        return self

    def catalog_hash(self) -> str:
        return sha256_hex(canonical_dumps(self.model_dump(mode="json")))


class LineItemStatus(StrEnum):
    priced = "priced"
    unpriced = "unpriced"


class CostLineItem(SpecModel):
    line_item_id: IdStr
    dataset_id: IdStr
    call_id: IdStr
    pricing_run_id: IdStr
    resource: Resource
    data_kind: DataKind
    quantity: NonNegInt | None = None
    unit: PriceUnit = PriceUnit.token
    unit_quantity: PosInt | None = None
    unit_price: DecimalStr | None = None
    currency: CurrencyStr | None = None
    line_cost: DecimalStr | None = Field(
        default=None, description="quantity * unit_price / unit_quantity; None when unpriced"
    )
    status: LineItemStatus
    unpriced_reason: CodeStr | None = None
    evidence_class: EvidenceClass
    catalog_version: VersionStr | None = None
    price_id: IdStr | None = None
    price_effective_from_ms: UtcMs | None = None
    price_effective_to_ms: UtcMs | None = None
    priced_at_ms: UtcMs | None = Field(
        default=None, description="Call timestamp used to select the price version"
    )
    boundary_call: bool = False
    model_id_priced: IdStr | None = None

    @model_validator(mode="after")
    def _status_shape(self) -> CostLineItem:
        if self.status is LineItemStatus.priced:
            if self.line_cost is None or self.unit_price is None or self.quantity is None:
                raise ValueError("priced line items need quantity, unit_price and line_cost")
        else:
            if self.line_cost is not None:
                raise ValueError("unpriced line items cannot carry a line_cost")
            if self.unpriced_reason is None:
                raise ValueError("unpriced line items need an unpriced_reason")
        return self


class PricingRunManifest(SpecModel):
    pricing_run_id: IdStr
    dataset_id: IdStr
    catalog_version: VersionStr
    catalog_hash: str = Field(max_length=128)
    catalog_kind: Literal["synthetic", "real"]
    normalizer_version: VersionStr
    created_at_ms: UtcMs
    call_count: NonNegInt
    calls_hash: str = Field(max_length=128, description="Hash over the priced call projections")
    priced_call_count: NonNegInt
    partially_priced_call_count: NonNegInt
    unpriced_call_count: NonNegInt
    line_item_count: NonNegInt
    known_cost_subtotal_usd: DecimalStr
    cost_complete: bool
