"""``estimate(call, catalog, pricing_run_id) -> list[CostLineItem]`` (PLAN.md section 6.2).

Pure function: no clock, no network, no database. The same inputs produce the same line
items with the same ids. Every priced token belongs to exactly one resource class; anything
that cannot be priced without guessing becomes an *unpriced* line item with a reason.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_EVEN, Decimal

from aiecon.pricing.catalog import CatalogIndex
from aiecon.spec.call import ModelCall, UsageCompleteness
from aiecon.spec.common import EvidenceClass
from aiecon.spec.pricing import (
    CostLineItem,
    LineItemStatus,
    PriceCatalog,
    PriceRecord,
    PriceUnit,
    Resource,
)

TWELVE_PLACES = Decimal("1.000000000000")
DAY_MS = 86_400_000

TIER_RESOURCES: dict[str, Resource] = {
    "ephemeral_5m": Resource.input_cache_write_5m,
    "ephemeral_1h": Resource.input_cache_write_1h,
}


def line_cost(quantity: int, record: PriceRecord) -> Decimal:
    """``quantity * unit_price / unit_quantity`` kept at 12 decimal places."""

    raw = Decimal(quantity) * record.unit_price / Decimal(record.unit_quantity)
    return raw.quantize(TWELVE_PLACES, rounding=ROUND_HALF_EVEN)


def _is_boundary(call: ModelCall) -> bool:
    if call.started_at_ms is None or call.ended_at_ms is None:
        return False
    return call.started_at_ms // DAY_MS != call.ended_at_ms // DAY_MS


class _Builder:
    def __init__(self, call: ModelCall, pricing_run_id: str, index: CatalogIndex, ts: int | None):
        self.call = call
        self.run_id = pricing_run_id
        self.index = index
        self.ts = ts
        self.boundary = _is_boundary(call)
        self.items: list[CostLineItem] = []
        self.model_id: str | None = None

    def _base(self, resource: Resource) -> dict:
        return {
            "line_item_id": f"li_{self.run_id}_{self.call.call_id}_{resource.value}",
            "dataset_id": self.call.dataset_id,
            "call_id": self.call.call_id,
            "pricing_run_id": self.run_id,
            "resource": resource,
            "data_kind": self.call.data_kind,
            "unit": PriceUnit.token,
            "evidence_class": EvidenceClass.B,
            "boundary_call": self.boundary,
            "priced_at_ms": self.ts,
            "model_id_priced": self.model_id,
            "catalog_version": self.index.catalog.catalog_version,
        }

    def unpriced(self, resource: Resource, quantity: int | None, reason: str) -> None:
        self.items.append(
            CostLineItem(
                **self._base(resource),
                quantity=quantity,
                status=LineItemStatus.unpriced,
                unpriced_reason=reason,
            )
        )

    def price(self, resource: Resource, quantity: int) -> None:
        if quantity == 0:
            return
        assert self.model_id is not None and self.ts is not None
        record = self.index.price_at(
            self.call.provider,
            self.model_id,
            resource,
            self.ts,
            api_family=self.call.api_family if self.call.api_family.value != "unknown" else None,
            service_tier=self.call.service_tier or "standard",
            region=self.call.inference_region or "global",
        )
        if record is None:
            self.unpriced(resource, quantity, "price_missing")
            return
        self.items.append(
            CostLineItem(
                **self._base(resource),
                quantity=quantity,
                unit_quantity=record.unit_quantity,
                unit_price=record.unit_price,
                currency=record.currency,
                line_cost=line_cost(quantity, record),
                status=LineItemStatus.priced,
                price_id=record.price_id,
                price_effective_from_ms=record.effective_from_ms,
                price_effective_to_ms=record.effective_to_ms,
            )
        )


def estimate(
    call: ModelCall, catalog: PriceCatalog | CatalogIndex, pricing_run_id: str
) -> list[CostLineItem]:
    index = catalog if isinstance(catalog, CatalogIndex) else CatalogIndex(catalog)
    ts = call.price_selection_ts_ms
    builder = _Builder(call, pricing_run_id, index, ts)

    model_id, _basis = index.resolve_model(call.provider, call.model_resolved, call.model_requested)
    builder.model_id = model_id

    if call.usage_completeness in (UsageCompleteness.missing, UsageCompleteness.invalid):
        reason = (
            "usage_missing"
            if call.usage_completeness is UsageCompleteness.missing
            else "usage_invalid"
        )
        builder.unpriced(Resource.input_uncached, call.input_total_tokens, reason)
        builder.unpriced(Resource.output, call.output_tokens, reason)
        return builder.items

    if model_id is None:
        builder.unpriced(Resource.input_uncached, call.input_total_tokens, "unknown_model")
        builder.unpriced(Resource.output, call.output_tokens, "unknown_model")
        return builder.items

    if ts is None:
        builder.unpriced(Resource.input_uncached, call.input_total_tokens, "timestamp_missing")
        builder.unpriced(Resource.output, call.output_tokens, "timestamp_missing")
        return builder.items

    # ------------------------------------------------------------------ input
    split_known = all(
        v is not None
        for v in (
            call.input_uncached_tokens,
            call.input_cache_read_tokens,
            call.input_cache_write_tokens,
        )
    )
    if not split_known:
        builder.unpriced(Resource.input_uncached, call.input_total_tokens, "input_split_unknown")
    else:
        assert call.input_uncached_tokens is not None
        assert call.input_cache_read_tokens is not None
        assert call.input_cache_write_tokens is not None
        builder.price(Resource.input_uncached, call.input_uncached_tokens)
        builder.price(Resource.input_cache_read, call.input_cache_read_tokens)
        _price_cache_writes(builder, call, index, model_id)

    # ----------------------------------------------------------------- output
    if call.output_tokens is None:
        builder.unpriced(Resource.output, None, "output_unknown")
    else:
        builder.price(Resource.output, call.output_tokens)
    return builder.items


def _price_cache_writes(
    builder: _Builder, call: ModelCall, index: CatalogIndex, model_id: str
) -> None:
    write_total = call.input_cache_write_tokens or 0
    contract = index.contract(call.provider, model_id)
    available = index.resources_for(call.provider, model_id)
    tiered_prices = {Resource.input_cache_write_5m, Resource.input_cache_write_1h} & available
    bills_writes = bool(tiered_prices or Resource.input_cache_write in available) or bool(
        contract and contract.cache_write_tiers
    )

    if write_total == 0:
        if "cache_write_field_absent" in call.usage_notes and bills_writes:
            # the counter was missing, so writes are hidden inside uncached: lower bound only
            builder.unpriced(Resource.input_cache_write, None, "cache_write_count_unknown")
        return

    if call.cache_write_breakdown:
        for tier, quantity in call.cache_write_breakdown.items():
            resource = TIER_RESOURCES.get(tier)
            if resource is None:
                builder.unpriced(
                    Resource.input_cache_write, quantity, "cache_write_tier_unsupported"
                )
            else:
                builder.price(resource, quantity)
        return

    # write total without tiers: only a single-tier price may be applied
    if Resource.input_cache_write in available and not tiered_prices:
        builder.price(Resource.input_cache_write, write_total)
    elif tiered_prices:
        builder.unpriced(Resource.input_cache_write, write_total, "cache_write_tier_unknown")
    else:
        builder.unpriced(Resource.input_cache_write, write_total, "price_missing")


def estimate_many(
    calls: Iterable[ModelCall], catalog: PriceCatalog | CatalogIndex, pricing_run_id: str
) -> list[CostLineItem]:
    index = catalog if isinstance(catalog, CatalogIndex) else CatalogIndex(catalog)
    items: list[CostLineItem] = []
    for call in calls:
        items.extend(estimate(call, index, pricing_run_id))
    return items
