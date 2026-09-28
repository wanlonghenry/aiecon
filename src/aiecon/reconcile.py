"""Usage and monetary reconciliation at the provider's real grain (PLAN.md §7.5–§7.6).

For every (provider, scope, UTC day[, model]) that has local calls or provider records
inside the requested window, two comparisons are produced:

* ``usage_comparison`` - local normalized token counts vs provider usage counts
* ``cost_comparison`` - E (local known-cost estimate for the same scope/day) vs B (provider
  reported cost, modeled categories only; unmodeled charges are listed separately)

Comparability rules enforced here:

* daily provider grains are compared over whole UTC days only; the window must start and
  end at UTC midnight
* one provider snapshot supplies a given day per (provider, scope, record kind): the latest
  complete fetch covering that day. Totals and line items are never added together, and a
  settled cost snapshot takes precedence over a provisional cost report
* a bucket whose local estimate is incomplete cannot be ``matched`` by tolerance; it is
  ``unpriced`` unless the known subtotal equals the provider amount exactly
* a capture gap is priced per model and per resource with that model's own line-item
  prices; it is evidence-backed only when the snapshot declares the scope dedicated to the
  captured traffic, otherwise it stays a hypothesis

Nothing here writes back to calls or line items. Tolerances only classify.
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
from aiecon.spec.provider import (
    NOT_GROUPED,
    Finality,
    ProviderRecord,
    ProviderSnapshotManifest,
    RecordKind,
)
from aiecon.spec.reconcile import (
    Adjustment,
    ComparisonKind,
    ReconciliationBucket,
    ReconciliationStatus,
)
from aiecon.storage import Storage, WorkspaceError

DAY_MS = 86_400_000
TWELVE = Decimal("1.000000000000")
PCT = Decimal("1.0000")

USAGE_METRICS = (
    "input_uncached_tokens",
    "input_cache_read_tokens",
    "input_cache_write_tokens",
    "input_total_tokens",
    "output_tokens",
)
TIER_METRICS = ("input_cache_write_5m_tokens", "input_cache_write_1h_tokens")
RESOURCE_FOR_METRIC: dict[str, tuple[Resource, ...]] = {
    "input_uncached_tokens": (Resource.input_uncached,),
    "input_cache_read_tokens": (Resource.input_cache_read,),
    "input_cache_write_5m_tokens": (Resource.input_cache_write_5m,),
    "input_cache_write_1h_tokens": (Resource.input_cache_write_1h,),
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


# --------------------------------------------------------- snapshot selection
@dataclass
class EffectiveRecords:
    records: list[ProviderRecord]
    snapshot_ids: list[str]
    manifests: dict[str, ProviderSnapshotManifest]


def effective_provider_records(
    storage: Storage, *, data_kind: DataKind, window: TimeWindow
) -> EffectiveRecords:
    """Pick, per (provider, scope, record kind, UTC day), the latest complete snapshot.

    Only that snapshot's records for the day count. A later re-pull of one day replaces
    that day only; other days keep their previous source. Rows that vanished from the
    latest pull vanish here. Different grains of the same kind never add up because only
    one snapshot supplies a day.
    """

    manifests = {m.snapshot_id: m for m in storage.list_snapshots(active_only=True)}
    all_records = [
        r
        for r in storage.list_provider_records(active_only=True)
        if r.data_kind is data_kind and r.snapshot_id in manifests
    ]
    chosen: dict[tuple[str, str, str, int], str] = {}
    first_day = (window.start_ms // DAY_MS) * DAY_MS
    last_day = ((window.end_ms - 1) // DAY_MS) * DAY_MS
    day = first_day
    while day <= last_day:
        for manifest in manifests.values():
            if manifest.data_kind is not data_kind or not manifest.snapshot_complete:
                continue
            if not (manifest.query_window.start_ms <= day < manifest.query_window.end_ms):
                continue
            key = (manifest.provider.value, manifest.scope_id, manifest.record_kind.value, day)
            current = chosen.get(key)
            if current is None or (
                manifests[current].fetched_at_ms,
                manifests[current].snapshot_id,
            ) < (manifest.fetched_at_ms, manifest.snapshot_id):
                chosen[key] = manifest.snapshot_id
        day += DAY_MS
    selected: list[ProviderRecord] = []
    used: set[str] = set()
    for record in all_records:
        record_day = (record.window_start_ms // DAY_MS) * DAY_MS
        key = (record.provider.value, record.scope_id, record.record_kind.value, record_day)
        if (
            chosen.get(key) == record.snapshot_id
            and window.start_ms <= record.window_start_ms < window.end_ms
        ):
            selected.append(record)
            used.add(record.snapshot_id)
    return EffectiveRecords(records=selected, snapshot_ids=sorted(used), manifests=manifests)


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

    @property
    def writes_without_tier(self) -> int:
        """Calls that wrote to the cache without a 5m/1h breakdown: their tier is unknown."""

        return sum(
            1
            for c in self.calls
            if (c.input_cache_write_tokens or 0) > 0 and not c.cache_write_breakdown
        )

    def usage(self) -> dict[str, int]:
        """Local totals per metric. Tier metrics are reported only when every local cache
        write carries its tier, otherwise they are not comparable with the provider's."""

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
            if call.cache_write_breakdown:
                totals["input_cache_write_5m_tokens"] += call.cache_write_breakdown.get(
                    "ephemeral_5m", 0
                )
                totals["input_cache_write_1h_tokens"] += call.cache_write_breakdown.get(
                    "ephemeral_1h", 0
                )
        if self.writes_without_tier:
            for metric in TIER_METRICS:
                totals.pop(metric, None)
        totals["requests"] = requests
        return dict(totals)

    def unit_price_for(self, resources: tuple[Resource, ...]) -> Decimal | None:
        """Per-token price observed in this side's own priced line items."""

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
        if model == NOT_GROUPED:
            if r.dimensions.get("model") not in (None, NOT_GROUPED):
                continue
        elif model is not None and r.dimensions.get("model") != model:
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


def _variance_pct(e: Decimal, b: Decimal) -> Decimal | None:
    if b <= 0:
        return None
    return ((e - b) / b * 100).quantize(PCT, rounding=ROUND_HALF_EVEN)


def _check_window(window: TimeWindow, records: list[ProviderRecord]) -> None:
    daily = any(r.grain.startswith("1d/") for r in records)
    if daily and (window.start_ms % DAY_MS or window.end_ms % DAY_MS):
        raise WorkspaceError(
            "daily provider grains can only be compared over whole UTC days; pass --start and "
            "--end as dates (YYYY-MM-DD) or datetimes at 00:00:00Z"
        )


@dataclass
class _GapItem:
    model: str
    metric: str
    tokens: int
    price: Decimal | None
    record_ids: list[str]


def reconcile(
    storage: Storage,
    dataset_id: str,
    window: TimeWindow,
    *,
    now_ms: int,
    settings: Settings = DEFAULT_SETTINGS,
) -> ReconcileResult:
    all_calls = storage.list_calls(dataset_id)
    data_kind = all_calls[0].data_kind if all_calls else DataKind.synthetic
    effective = effective_provider_records(storage, data_kind=data_kind, window=window)
    records = effective.records
    _check_window(window, records)
    calls = [
        c for c in all_calls if c.started_at_ms is not None and window.contains(c.started_at_ms)
    ]
    pricing = storage.active_pricing_run(dataset_id)
    line_items = storage.list_line_items(pricing.pricing_run_id) if pricing else []
    items_by_call: dict[str, list[CostLineItem]] = defaultdict(list)
    for li in line_items:
        items_by_call[li.call_id].append(li)

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
        scope_days.add((r.provider.value, r.scope_id, (r.window_start_ms // DAY_MS) * DAY_MS))
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
        has_local = scope in local_scopes.get(provider, set())
        has_provider = scope in provider_scopes.get(provider, set())
        scope_mismatch = (
            (not has_local or not has_provider)
            and bool(local_scopes.get(provider))
            and bool(provider_scopes.get(provider))
        )
        # a surplus is evidence only when every snapshot feeding this day declares that the
        # provider scope carries nothing but the captured traffic
        dedicated = bool(day_records) and all(
            effective.manifests[r.snapshot_id].scope_dedicated is True for r in day_records
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
            if any(r.dimensions.get("model") in (None, NOT_GROUPED) for r in usage_records):
                models.add(NOT_GROUPED)  # rows without a model are compared, not dropped
            model_keys = sorted(models)
        else:
            model_keys = [None]
        gap_items: list[_GapItem] = []
        gap_unpriceable = False
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
                # with no usable local call every provider-reported token is surplus; with
                # local calls a metric they never carried stays non-comparable
                no_local = local_usage.get("requests", 0) == 0
                for metric in (*USAGE_METRICS, *TIER_METRICS, "requests"):
                    if metric in provider_usage and (metric in local_usage or no_local):
                        usage_variance[metric] = local_usage.get(metric, 0) - provider_usage[metric]
                comparable = [m for m in usage_variance if m != "requests"]
                if not comparable:
                    status = ReconciliationStatus.no_provider_usage
                    reasons.append("no_common_usage_metrics")
                else:
                    status = (
                        ReconciliationStatus.matched
                        if all(v == 0 for v in usage_variance.values())
                        else ReconciliationStatus.variance
                    )
                if any(v < 0 for v in usage_variance.values()):
                    reasons.append("capture_gap" if dedicated else "provider_exceeds_local")
                    # price the surplus per metric with THIS model's own prices; the
                    # aggregate write metric is only used when no tier split exists
                    tiers_present = any(m in usage_variance for m in TIER_METRICS)
                    for metric, delta in usage_variance.items():
                        if delta >= 0 or metric == "requests":
                            continue
                        if metric == "input_total_tokens":
                            continue
                        if metric == "input_cache_write_tokens":
                            if tiers_present:
                                continue
                            gap_unpriceable = True  # tier unknown: never guess a write price
                            continue
                        if model is None:
                            gap_unpriceable = True
                            continue
                        price = side.unit_price_for(RESOURCE_FOR_METRIC[metric])
                        if price is None:
                            gap_unpriceable = True
                        gap_items.append(
                            _GapItem(
                                model=model,
                                metric=metric,
                                tokens=-delta,
                                price=price,
                                record_ids=[r.record_id for r in model_usage_records],
                            )
                        )
                if any(v > 0 for v in usage_variance.values()):
                    reasons.append("local_exceeds_provider")
                if side.unpriced_call_count:
                    reasons.append("unpriced_calls")
                if side.writes_without_tier and any(m in provider_usage for m in TIER_METRICS):
                    reasons.append("cache_write_tier_unknown")  # tier metrics not compared
                if any(r.finality is Finality.provisional for r in model_usage_records):
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
                    finality=(
                        model_usage_records[0].finality if model_usage_records else Finality.unknown
                    ),
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
        settled = [r for r in day_records if r.record_kind is RecordKind.settled_cost]
        reported = [r for r in day_records if r.record_kind is RecordKind.provider_cost]
        cost_records = settled or reported  # never both: settlement supersedes the report
        modeled = [r for r in cost_records if not _is_unmodeled(r)]
        unmodeled = [r for r in cost_records if _is_unmodeled(r)]
        b_values = [r.amount_usd for r in modeled if r.amount_usd is not None]
        non_usd = [r for r in modeled if r.amount_usd is None and r.amount_original is not None]
        e_value = _q(merged_local.estimate)
        complete = merged_local.cost_complete
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
            if settled:
                reasons.append("settled_cost")
            if non_usd:
                reasons.append("unsupported_charge")
            if not merged_local.calls:
                reasons.append("no_local_calls")
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
            if gap_items:
                priced = [g for g in gap_items if g.price is not None]
                gap_cost = sum((Decimal(g.tokens) * g.price for g in priced), start=Decimal(0))  # type: ignore[operator]
                refs = sorted({rid for g in gap_items for rid in g.record_ids})
                fully_priced = not gap_unpriceable and len(priced) == len(gap_items)
                if gap_cost > 0:
                    detail = "; ".join(f"{g.model}: {g.tokens} {g.metric}" for g in priced)[:300]
                    if dedicated and fully_priced:
                        explained.append(
                            Adjustment(
                                code="capture_gap",
                                signed_amount_usd=_q(-gap_cost),
                                evidence_backed=True,
                                evidence_refs=refs,
                                description=(
                                    "Provider usage reports more tokens than the local log "
                                    "captured in a dedicated scope; priced per model with that "
                                    f"model's own line-item rates ({detail})."
                                ),
                            )
                        )
                        reasons.append("capture_gap")
                    else:
                        why = (
                            "scope not declared dedicated: the surplus may be other traffic"
                            if not dedicated
                            else "part of the surplus has no usable price (model or tier unknown)"
                        )
                        hypotheses.append(
                            Adjustment(
                                code="capture_gap",
                                signed_amount_usd=_q(-gap_cost),
                                evidence_backed=False,
                                evidence_refs=refs,
                                description=(
                                    f"Provider usage exceeds the local log ({detail}); {why}."
                                ),
                            )
                        )
                        reasons.append("capture_gap" if dedicated else "other_traffic_possible")
                elif gap_unpriceable:
                    reasons.append("capture_gap_unpriced")
            if any(r.finality is Finality.provisional for r in cost_records):
                reasons.append("late_data")
            explained_total = sum((a.signed_amount_usd for a in explained), start=Decimal(0))
            unexplained = _q(signed - explained_total)
            if signed == 0:
                status = ReconciliationStatus.matched
            elif absolute <= tolerance and complete:
                status = ReconciliationStatus.matched
            elif absolute <= tolerance:
                status = ReconciliationStatus.unpriced  # cannot claim a match on a lower bound
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
            local_cost_complete=complete if merged_local.calls else None,
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
                ",".join(effective.snapshot_ids),
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
        provider_snapshot_ids=effective.snapshot_ids,
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
    """The fixed one-line summary appended after the reconcile table (PLAN.md §7.6).

    The percentage sentence is printed only for comparable buckets: a completed monetary
    comparison with B > 0 whose status is matched or variance. A known-cost lower bound
    (calls with unknown cost) keeps the sentence but says so; provisional provider data is
    labeled. Every other bucket prints its primary status and the reason instead.
    """

    window = f"{day_label(bucket.window_start_ms)} UTC"
    where = f"({bucket.provider.value}, {bucket.scope_id}, {window})"
    qualifiers: list[str] = []
    if bucket.local_cost_complete is False:
        qualifiers.append(
            f"known-cost lower bound: {bucket.local_unpriced_call_count} call(s) with unknown cost"
        )
    if "late_data" in bucket.reasons:
        qualifiers.append("provider data provisional")
    suffix = f" [{'; '.join(qualifiers)}]" if qualifiers else ""
    if (
        bucket.comparison_kind is ComparisonKind.cost_comparison
        and bucket.provider_cost_usd is not None
        and bucket.provider_cost_usd > 0
        and bucket.variance_pct is not None
        and bucket.status in (ReconciliationStatus.matched, ReconciliationStatus.variance)
    ):
        direction = "below" if bucket.variance_pct < 0 else "above"
        pct = abs(bucket.variance_pct).quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN)
        return f"Your estimates run {pct}% {direction} provider-reported costs {where}.{suffix}"
    status = bucket.status.value
    if bucket.status is ReconciliationStatus.no_provider_cost:
        status = "no_provider_cost — live reconciliation pending"
    elif bucket.reasons:
        status = f"{status} — {', '.join(bucket.reasons)}"
    return f"{status} {where}{suffix}"
