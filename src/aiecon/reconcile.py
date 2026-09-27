"""Usage and monetary reconciliation at the provider's real grain (PLAN.md §7.5–§7.6).

For every (provider, scope, UTC day[, model]) that has local calls or active provider
records inside the requested window, two comparisons are produced:

* ``usage_comparison`` - local normalized token counts vs provider usage counts
* ``cost_comparison`` - E (local known-cost estimate for the same scope/day) vs B (provider
  reported cost, modeled categories only; unmodeled charges are listed separately)

Nothing here writes back to calls or line items. Evidence-backed adjustments explain part of
a variance; the remainder stays ``unexplained_delta_usd``. Tolerances only classify.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from pydantic import Field

from aiecon.config import DEFAULT_SETTINGS, Settings
from aiecon.spec.call import ModelCall, UsageCompleteness
from aiecon.spec.common import (
    DataKind,
    DecimalStr,
    IdStr,
    NonNegInt,
    Provider,
    SpecModel,
    TimeWindow,
    UtcMs,
    VersionStr,
)
from aiecon.spec.pricing import CostLineItem, LineItemStatus, Resource
from aiecon.spec.provider import NOT_GROUPED, Finality, ProviderRecord, RecordKind
from aiecon.spec.reconcile import (
    Adjustment,
    ComparisonKind,
    ReconciliationBucket,
    ReconciliationStatus,
)
from aiecon.storage import Storage

DAY_MS = 86_400_000
PROVISIONAL_MARGIN_MS = 6 * 3_600_000
TWELVE = Decimal("1.000000000000")
PCT = Decimal("1.0000")

USAGE_METRICS = (
    "input_uncached_tokens",
    "input_cache_read_tokens",
    "input_cache_write_tokens",
    "input_total_tokens",
    "output_tokens",
)
RESOURCE_FOR_METRIC: dict[str, tuple[Resource, ...]] = {
    "input_uncached_tokens": (Resource.input_uncached,),
    "input_cache_read_tokens": (Resource.input_cache_read,),
    "input_cache_write_tokens": (
        Resource.input_cache_write,
        Resource.input_cache_write_5m,
        Resource.input_cache_write_1h,
    ),
    "output_tokens": (Resource.output,),
}
UNMODELED_COST_TYPES = {"web_search", "code_execution", "session_usage"}
UNMODELED_LINE_ITEM_HINTS = (
    "image",
    "audio",
    "web_search",
    "file_search",
    "code_interpreter",
    "vector",
)


class ReconcileRunManifest(SpecModel):
    reconcile_run_id: IdStr
    dataset_id: IdStr
    data_kind: DataKind
    pricing_run_id: IdStr | None
    provider_snapshot_ids: list[IdStr]
    window: TimeWindow
    tolerance_abs_usd: DecimalStr
    tolerance_pct: DecimalStr
    created_at_ms: UtcMs
    normalizer_version: VersionStr | None = None
    bucket_count: NonNegInt
    status_counts: dict[str, int] = Field(default_factory=dict)
    total_local_estimate_usd: DecimalStr | None = None
    total_provider_cost_usd: DecimalStr | None = None
    total_unmodeled_charges_usd: DecimalStr | None = None


@dataclass
class ReconcileResult:
    manifest: ReconcileRunManifest
    buckets: list[ReconciliationBucket]
    summary_lines: list[str] = field(default_factory=list)


def day_label(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def _q(value: Decimal) -> Decimal:
    return value.quantize(TWELVE, rounding=ROUND_HALF_EVEN)


def _is_unmodeled(record: ProviderRecord) -> bool:
    cost_type = record.dimensions.get("cost_type")
    if cost_type is not None and cost_type in UNMODELED_COST_TYPES:
        return True
    line_item = (record.dimensions.get("line_item") or "").lower()
    return any(hint in line_item for hint in UNMODELED_LINE_ITEM_HINTS)


# ------------------------------------------------------------------ local side
@dataclass
class LocalSide:
    calls: list[ModelCall] = field(default_factory=list)
    line_items: list[CostLineItem] = field(default_factory=list)

    @property
    def estimate(self) -> Decimal:
        return sum(
            (li.line_cost for li in self.line_items if li.line_cost is not None), start=Decimal(0)
        )

    @property
    def cost_complete(self) -> bool:
        if not self.calls:
            return True
        priced_calls = {li.call_id for li in self.line_items}
        return all(
            c.usage_completeness is UsageCompleteness.complete and c.call_id in priced_calls
            for c in self.calls
        ) and all(li.status is LineItemStatus.priced for li in self.line_items)

    @property
    def unpriced_call_count(self) -> int:
        unpriced = {li.call_id for li in self.line_items if li.status is LineItemStatus.unpriced}
        priced_calls = {li.call_id for li in self.line_items}
        return sum(1 for c in self.calls if c.call_id in unpriced or c.call_id not in priced_calls)

    @property
    def unattributed_call_count(self) -> int:
        return sum(1 for c in self.calls if c.workflow_run_id is None)

    @property
    def boundary_calls(self) -> list[ModelCall]:
        return [
            c
            for c in self.calls
            if c.started_at_ms is not None
            and c.ended_at_ms is not None
            and c.started_at_ms // DAY_MS != c.ended_at_ms // DAY_MS
        ]

    def usage(self) -> dict[str, int]:
        totals: dict[str, int] = defaultdict(int)
        requests = 0
        for call in self.calls:
            if call.usage_completeness in (UsageCompleteness.missing, UsageCompleteness.invalid):
                continue
            requests += 1
            for metric in USAGE_METRICS:
                value = getattr(call, metric)
                if value is not None:
                    totals[metric] += value
        totals["requests"] = requests
        return dict(totals)

    def unit_price_for(self, resources: tuple[Resource, ...]) -> Decimal | None:
        """Per-token price observed in this bucket's own priced line items."""

        for li in self.line_items:
            if li.resource in resources and li.status is LineItemStatus.priced and li.unit_price:
                return li.unit_price / Decimal(li.unit_quantity or 1)
        return None


# ----------------------------------------------------------------- reconcile
def _bucket_key(
    kind: ComparisonKind, provider: str, scope: str, day: str, model: str | None
) -> str:
    tail = f"/{model}" if model else ""
    return f"{kind.value}/{provider}/{scope}/{day}{tail}"


def _records_for(
    records: list[ProviderRecord], provider: str, scope: str, day_start: int, model: str | None
) -> list[ProviderRecord]:
    out = []
    for r in records:
        if r.provider.value != provider or r.scope_id != scope:
            continue
        if not (day_start <= r.window_start_ms < day_start + DAY_MS):
            continue
        if model is not None and r.dimensions.get("model") != model:
            continue
        out.append(r)
    return out


def _provider_has_model_dim(
    records: list[ProviderRecord], provider: str, scope: str, kind: RecordKind
) -> bool:
    return any(
        r.provider.value == provider
        and r.scope_id == scope
        and r.record_kind is kind
        and r.dimensions.get("model") not in (None, NOT_GROUPED)
        for r in records
    )


def _provisional(records: list[ProviderRecord], day_start: int) -> bool:
    return any(
        r.finality is Finality.provisional
        and r.fetched_at_ms < day_start + DAY_MS + PROVISIONAL_MARGIN_MS
        for r in records
    )


def _variance_pct(e: Decimal, b: Decimal) -> Decimal | None:
    if b <= 0:
        return None
    return ((e - b) / b * 100).quantize(PCT, rounding=ROUND_HALF_EVEN)


def reconcile(
    storage: Storage,
    dataset_id: str,
    window: TimeWindow,
    *,
    now_ms: int,
    settings: Settings = DEFAULT_SETTINGS,
) -> ReconcileResult:
    calls = [
        c
        for c in storage.list_calls(dataset_id)
        if c.started_at_ms is not None and window.contains(c.started_at_ms)
    ]
    data_kind = calls[0].data_kind if calls else DataKind.synthetic
    pricing = storage.active_pricing_run(dataset_id)
    line_items = storage.list_line_items(pricing.pricing_run_id) if pricing else []
    items_by_call: dict[str, list[CostLineItem]] = defaultdict(list)
    for li in line_items:
        items_by_call[li.call_id].append(li)
    records = [
        r
        for r in storage.list_provider_records()
        if r.data_kind is data_kind
        and r.window_start_ms < window.end_ms
        and r.window_end_ms > window.start_ms
    ]
    snapshot_ids = sorted({r.snapshot_id for r in records})

    # local groups: (provider, scope, day_start, model)
    local: dict[tuple[str, str, int, str], LocalSide] = defaultdict(LocalSide)
    for c in calls:
        assert c.started_at_ms is not None
        model = c.model_resolved or c.model_requested
        side = local[(c.provider.value, c.scope_id, (c.started_at_ms // DAY_MS) * DAY_MS, model)]
        side.calls.append(c)
        side.line_items.extend(items_by_call.get(c.call_id, []))

    scope_days: set[tuple[str, str, int]] = {(p, s, d) for (p, s, d, _m) in local}
    for r in records:
        day_start = (r.window_start_ms // DAY_MS) * DAY_MS
        scope_days.add((r.provider.value, r.scope_id, day_start))
    local_scopes: dict[str, set[str]] = defaultdict(set)
    provider_scopes: dict[str, set[str]] = defaultdict(set)
    for p, s, _d, _m in local:
        local_scopes[p].add(s)
    for r in records:
        provider_scopes[r.provider.value].add(r.scope_id)

    buckets: list[ReconciliationBucket] = []
    summary: list[str] = []
    for provider, scope, day_start in sorted(scope_days):
        day = day_label(day_start)
        day_window = TimeWindow(start_ms=day_start, end_ms=day_start + DAY_MS)
        provider_enum = Provider(provider)
        day_records = _records_for(records, provider, scope, day_start, None)
        scope_mismatch = (
            scope not in local_scopes.get(provider, set())
            and bool(provider_scopes.get(provider))
            and bool(local_scopes.get(provider))
        ) or (
            scope not in provider_scopes.get(provider, set())
            and bool(provider_scopes.get(provider))
            and bool(local_scopes.get(provider))
        )
        merged_local = LocalSide()
        for (p, s, d, _m), side in local.items():
            if (p, s, d) == (provider, scope, day_start):
                merged_local.calls.extend(side.calls)
                merged_local.line_items.extend(side.line_items)

        # ------------------------------------------------- usage comparisons
        usage_records = [r for r in day_records if r.record_kind is RecordKind.provider_usage]
        by_model = _provider_has_model_dim(records, provider, scope, RecordKind.provider_usage)
        model_keys: list[str | None]
        if by_model:
            models = {m for (p, s, d, m) in local if (p, s, d) == (provider, scope, day_start)}
            models |= {
                r.dimensions.get("model") or NOT_GROUPED
                for r in usage_records
                if r.dimensions.get("model") not in (None, NOT_GROUPED)
            }
            model_keys = sorted(models)
        else:
            model_keys = [None]
        usage_deltas_for_cost: dict[str, int] = defaultdict(int)
        usage_evidence: list[str] = []
        for model in model_keys:
            side = LocalSide()
            for (p, s, d, m), part in local.items():
                if (p, s, d) == (provider, scope, day_start) and (model is None or m == model):
                    side.calls.extend(part.calls)
                    side.line_items.extend(part.line_items)
            model_records = _records_for(records, provider, scope, day_start, model)
            model_usage_records = [
                r for r in model_records if r.record_kind is RecordKind.provider_usage
            ]
            provider_usage: dict[str, int] = defaultdict(int)
            for r in model_usage_records:
                for k, v in (r.usage or {}).items():
                    provider_usage[k] += v
            local_usage = side.usage()
            reasons: list[str] = []
            usage_variance: dict[str, int] | None = None
            if scope_mismatch:
                status = ReconciliationStatus.scope_mismatch
                reasons.append("scope_mismatch")
            elif not model_usage_records:
                status = ReconciliationStatus.no_provider_usage
            else:
                usage_variance = {}
                for metric in (*USAGE_METRICS, "requests"):
                    if metric in provider_usage and metric in local_usage:
                        usage_variance[metric] = local_usage[metric] - provider_usage[metric]
                status = (
                    ReconciliationStatus.matched
                    if all(v == 0 for v in usage_variance.values())
                    else ReconciliationStatus.variance
                )
                if any(v < 0 for v in usage_variance.values()):
                    reasons.append("capture_gap")
                    for metric, delta in usage_variance.items():
                        if delta < 0 and metric in RESOURCE_FOR_METRIC:
                            usage_deltas_for_cost[metric] += -delta
                    usage_evidence.extend(r.record_id for r in model_usage_records)
                if any(v > 0 for v in usage_variance.values()):
                    reasons.append("local_exceeds_provider")
                if side.unpriced_call_count:
                    reasons.append("unpriced_calls")
                if _provisional(model_usage_records, day_start):
                    reasons.append("late_data")
            buckets.append(
                ReconciliationBucket(
                    reconcile_run_id="pending",
                    bucket_key=_bucket_key(
                        ComparisonKind.usage_comparison, provider, scope, day, model
                    ),
                    comparison_kind=ComparisonKind.usage_comparison,
                    dataset_id=dataset_id,
                    data_kind=data_kind,
                    provider=provider_enum,
                    scope_id=scope,
                    window_start_ms=day_window.start_ms,
                    window_end_ms=day_window.end_ms,
                    grain=model_usage_records[0].grain if model_usage_records else "1d/",
                    dimensions={"day": day, "model": model or NOT_GROUPED},
                    record_kind=RecordKind.provider_usage,
                    finality=model_usage_records[0].finality
                    if model_usage_records
                    else Finality.unknown,
                    pricing_run_id=pricing.pricing_run_id if pricing else None,
                    local_call_count=len(side.calls),
                    local_unpriced_call_count=side.unpriced_call_count,
                    local_unattributed_call_count=side.unattributed_call_count,
                    local_usage=local_usage or None,
                    provider_snapshot_ids=sorted({r.snapshot_id for r in model_usage_records}),
                    provider_record_ids=[r.record_id for r in model_usage_records],
                    provider_usage=dict(provider_usage) or None,
                    usage_variance=usage_variance,
                    status=status,
                    reasons=reasons,
                )
            )

        # -------------------------------------------------- cost comparison
        cost_records = [
            r
            for r in day_records
            if r.record_kind in (RecordKind.provider_cost, RecordKind.settled_cost)
        ]
        modeled = [r for r in cost_records if not _is_unmodeled(r)]
        unmodeled = [r for r in cost_records if _is_unmodeled(r)]
        b_values = [r.amount_usd for r in modeled if r.amount_usd is not None]
        non_usd = [r for r in modeled if r.amount_usd is None and r.amount_original is not None]
        e_value = _q(merged_local.estimate)
        reasons = []
        explained: list[Adjustment] = []
        hypotheses: list[Adjustment] = []
        signed = absolute = pct = tolerance = unexplained = None
        b_value: Decimal | None = None
        if scope_mismatch:
            status = ReconciliationStatus.scope_mismatch
            reasons.append("scope_mismatch")
        elif not cost_records:
            status = ReconciliationStatus.no_provider_cost
        else:
            b_value = _q(sum(b_values, start=Decimal(0))) if b_values else Decimal(0)
            signed = _q(e_value - b_value)
            absolute = abs(signed)
            pct = _variance_pct(e_value, b_value)
            tolerance = max(settings.tolerance_abs_usd, settings.tolerance_pct / 100 * abs(b_value))
            if non_usd:
                reasons.append("unsupported_charge")
            if merged_local.unpriced_call_count:
                reasons.append("unpriced_calls")
            if merged_local.boundary_calls:
                reasons.append("boundary_call")
                boundary_cost = sum(
                    (
                        li.line_cost
                        for c in merged_local.boundary_calls
                        for li in items_by_call.get(c.call_id, [])
                        if li.line_cost is not None
                    ),
                    start=Decimal(0),
                )
                hypotheses.append(
                    Adjustment(
                        code="boundary_call",
                        signed_amount_usd=_q(boundary_cost),
                        evidence_backed=False,
                        evidence_refs=[c.call_id for c in merged_local.boundary_calls],
                        description=(
                            "Calls straddling UTC midnight are priced on their start day; the "
                            "provider may report them on the end day."
                        ),
                    )
                )
            if usage_deltas_for_cost:
                gap_cost = Decimal(0)
                priced_all = True
                for metric, tokens in usage_deltas_for_cost.items():
                    price = merged_local.unit_price_for(RESOURCE_FOR_METRIC[metric])
                    if price is None:
                        priced_all = False
                        continue
                    gap_cost += Decimal(tokens) * price
                if gap_cost > 0:
                    adjustment = Adjustment(
                        code="capture_gap",
                        signed_amount_usd=_q(-gap_cost),
                        evidence_backed=priced_all,
                        evidence_refs=sorted(set(usage_evidence)),
                        description=(
                            "Provider usage reports more tokens than the local log captured; "
                            "priced at the rates of this bucket's own line items."
                        ),
                    )
                    (explained if priced_all else hypotheses).append(adjustment)
                reasons.append("capture_gap")
            if _provisional(cost_records, day_start):
                reasons.append("late_data")
            explained_total = sum((a.signed_amount_usd for a in explained), start=Decimal(0))
            unexplained = _q(signed - explained_total)
            if _provisional(cost_records, day_start) and absolute > tolerance:
                status = ReconciliationStatus.provisional
            elif absolute <= tolerance:
                status = ReconciliationStatus.matched
            else:
                status = ReconciliationStatus.variance
            if status is ReconciliationStatus.variance and abs(unexplained) <= tolerance:
                reasons.append("explained")
            elif status is ReconciliationStatus.variance:
                reasons.append("unexplained")
        unmodeled_total = sum(
            (r.amount_usd for r in unmodeled if r.amount_usd is not None), start=Decimal(0)
        )
        bucket = ReconciliationBucket(
            reconcile_run_id="pending",
            bucket_key=_bucket_key(ComparisonKind.cost_comparison, provider, scope, day, None),
            comparison_kind=ComparisonKind.cost_comparison,
            dataset_id=dataset_id,
            data_kind=data_kind,
            provider=provider_enum,
            scope_id=scope,
            window_start_ms=day_window.start_ms,
            window_end_ms=day_window.end_ms,
            grain=cost_records[0].grain if cost_records else "1d/",
            dimensions={"day": day, "model": NOT_GROUPED},
            record_kind=cost_records[0].record_kind if cost_records else None,
            finality=cost_records[0].finality if cost_records else Finality.unknown,
            pricing_run_id=pricing.pricing_run_id if pricing else None,
            local_call_count=len(merged_local.calls),
            local_unpriced_call_count=merged_local.unpriced_call_count,
            local_unattributed_call_count=merged_local.unattributed_call_count,
            local_usage=merged_local.usage() or None,
            local_estimate_usd=e_value if merged_local.calls else None,
            local_cost_complete=merged_local.cost_complete if merged_local.calls else None,
            provider_snapshot_ids=sorted({r.snapshot_id for r in cost_records}),
            provider_record_ids=[r.record_id for r in cost_records],
            provider_cost_usd=b_value,
            signed_variance_usd=signed,
            absolute_variance_usd=absolute,
            variance_pct=pct,
            tolerance_usd=_q(tolerance) if tolerance is not None else None,
            status=status,
            reasons=reasons,
            explained_adjustments=explained,
            hypotheses=hypotheses,
            unexplained_delta_usd=unexplained,
            unmodeled_charges_usd=_q(unmodeled_total) if unmodeled else None,
        )
        buckets.append(bucket)
        summary.append(shareable_line(bucket))

    digest = hashlib.sha256(
        "|".join(
            [
                dataset_id,
                pricing.pricing_run_id if pricing else "-",
                ",".join(snapshot_ids),
                str(window.start_ms),
                str(window.end_ms),
                str(settings.tolerance_abs_usd),
                str(settings.tolerance_pct),
            ]
        ).encode()
    ).hexdigest()
    run_id = f"rr_{digest[:16]}"
    buckets = [b.model_copy(update={"reconcile_run_id": run_id}) for b in buckets]
    status_counts: dict[str, int] = defaultdict(int)
    for b in buckets:
        status_counts[f"{b.comparison_kind.value}:{b.status.value}"] += 1
    cost_buckets = [b for b in buckets if b.comparison_kind is ComparisonKind.cost_comparison]
    manifest = ReconcileRunManifest(
        reconcile_run_id=run_id,
        dataset_id=dataset_id,
        data_kind=data_kind,
        pricing_run_id=pricing.pricing_run_id if pricing else None,
        provider_snapshot_ids=snapshot_ids,
        window=window,
        tolerance_abs_usd=settings.tolerance_abs_usd,
        tolerance_pct=settings.tolerance_pct,
        created_at_ms=now_ms,
        normalizer_version=pricing.normalizer_version if pricing else None,
        bucket_count=len(buckets),
        status_counts=dict(sorted(status_counts.items())),
        total_local_estimate_usd=_q(
            sum((b.local_estimate_usd or Decimal(0) for b in cost_buckets), start=Decimal(0))
        ),
        total_provider_cost_usd=(
            _q(
                sum(
                    (b.provider_cost_usd for b in cost_buckets if b.provider_cost_usd is not None),
                    start=Decimal(0),
                )
            )
            if any(b.provider_cost_usd is not None for b in cost_buckets)
            else None
        ),
        total_unmodeled_charges_usd=(
            _q(
                sum(
                    (
                        b.unmodeled_charges_usd
                        for b in cost_buckets
                        if b.unmodeled_charges_usd is not None
                    ),
                    start=Decimal(0),
                )
            )
            if any(b.unmodeled_charges_usd is not None for b in cost_buckets)
            else None
        ),
    )
    with storage.transaction():
        storage.replace_buckets(run_id, buckets)
        storage.register_run(
            run_id=run_id,
            run_kind="reconcile",
            dataset_id=dataset_id,
            created_at_ms=now_ms,
            manifest_json=manifest.model_dump_json(),
            activate=True,
        )
    return ReconcileResult(manifest=manifest, buckets=buckets, summary_lines=summary)


def shareable_line(bucket: ReconciliationBucket) -> str:
    """The fixed one-line summary appended after the reconcile table (PLAN.md §7.6)."""

    window = f"{day_label(bucket.window_start_ms)} UTC"
    where = f"({bucket.provider.value}, {bucket.scope_id}, {window})"
    if (
        bucket.comparison_kind is ComparisonKind.cost_comparison
        and bucket.provider_cost_usd is not None
        and bucket.provider_cost_usd > 0
        and bucket.variance_pct is not None
        and bucket.status in (ReconciliationStatus.matched, ReconciliationStatus.variance)
    ):
        direction = "below" if bucket.variance_pct < 0 else "above"
        pct = abs(bucket.variance_pct).quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN)
        return f"Your estimates run {pct}% {direction} provider-reported costs {where}."
    status = bucket.status.value
    if bucket.status is ReconciliationStatus.no_provider_cost:
        status = "no_provider_cost — live reconciliation pending"
    return f"{status} {where}"
