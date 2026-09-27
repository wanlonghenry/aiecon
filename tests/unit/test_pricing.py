"""T2.1: table-driven pricing cases (V09, V11, V12, section 13.1) and pricing-run idempotency."""

from __future__ import annotations

from decimal import Decimal

import pytest

from aiecon import __version__
from aiecon.pricing import CatalogIndex, estimate, load_synthetic_catalog
from aiecon.pricing.run import run_pricing
from aiecon.spec import (
    ApiFamily,
    CallStatus,
    DataKind,
    LineItemStatus,
    ModelCall,
    PriceCatalog,
    Provider,
    Resource,
    UsageCompleteness,
    UsageFormat,
)
from aiecon.storage import Storage, Workspace

M = 1_000_000
T_CALL = 1_790_467_200_000  # 2026-09-27T00:00:00Z
T_SWITCH = T_CALL + 3_600_000  # a price change one hour later


def record(
    model: str,
    resource: str,
    price: str,
    *,
    provider: str = "anthropic",
    start: int = 0,
    end: int | None = None,
    api_family: str | None = None,
    price_id: str | None = None,
) -> dict:
    return {
        "price_id": price_id or f"{model}-{resource}-{start}-{api_family or 'any'}",
        "catalog_version": "test-0.1",
        "provider": provider,
        "model_id": model,
        "api_family": api_family,
        "resource": resource,
        "effective_from_ms": start,
        "effective_to_ms": end,
        "unit": "token",
        "unit_price": price,
        "source_url": "synthetic://test",
        "retrieved_at": "2026-09-26",
        "effective_date_basis": "synthetic",
    }


# section 13.1 rates: input 2, cache read 0.5, cache write 2.5, output 8 (per million)
SAMPLE_RATES = [
    record("fixture-a", "input_uncached", "2"),
    record("fixture-a", "input_cache_read", "0.5"),
    record("fixture-a", "input_cache_write_5m", "2.5"),
    record("fixture-a", "input_cache_write_1h", "4"),
    record("fixture-a", "output", "8"),
    record("fixture-o", "input_uncached", "2", provider="openai"),
    record("fixture-o", "input_cache_read", "0.5", provider="openai"),
    record("fixture-o", "input_cache_write", "2.5", provider="openai"),
    record("fixture-o", "output", "8", provider="openai"),
]


def catalog(records: list[dict], **extra) -> CatalogIndex:
    data = {
        "catalog_version": "test-0.1",
        "catalog_kind": "synthetic",
        "description": "test",
        "records": records,
        "aliases": extra.get("aliases", []),
        "cache_contracts": extra.get("cache_contracts", []),
    }
    return CatalogIndex(PriceCatalog.model_validate(data))


def call(
    *,
    provider: str = "anthropic",
    model: str = "fixture-a",
    uncached: int | None = 300,
    read: int | None = 600,
    write: int | None = 100,
    output: int | None = 100,
    breakdown: dict[str, int] | None = None,
    completeness: str = "complete",
    started: int | None = T_CALL,
    ended: int | None = T_CALL + 1000,
    notes: list[str] | None = None,
    api_family: str = "unknown",
    call_id: str = "c1",
) -> ModelCall:
    total = None
    if None not in (uncached, read, write):
        total = uncached + read + write  # type: ignore[operator]
    return ModelCall(
        dataset_id="d",
        call_id=call_id,
        data_kind=DataKind.synthetic,
        scope_id="s",
        normalizer_version="0.1.0",
        provider=Provider(provider),
        api_family=ApiFamily(api_family),
        model_requested=model,
        model_resolved=model,
        status=CallStatus.success,
        started_at_ms=started,
        ended_at_ms=ended,
        usage_format=UsageFormat.anthropic_messages,
        input_total_tokens=total,
        input_uncached_tokens=uncached,
        input_cache_read_tokens=read,
        input_cache_write_tokens=write,
        output_tokens=output,
        cache_write_breakdown=breakdown,
        usage_completeness=UsageCompleteness(completeness),
        usage_notes=notes or [],
    )


def total_cost(items) -> Decimal:
    return sum((i.line_cost for i in items if i.line_cost is not None), start=Decimal(0))


def by_resource(items):
    return {i.resource: i for i in items}


# --------------------------------------------------------------- section 13.1
def test_sample_anthropic_shape_prices_to_0_00195() -> None:
    items = estimate(call(breakdown={"ephemeral_5m": 100}), catalog(SAMPLE_RATES), "pr")
    assert all(i.status is LineItemStatus.priced for i in items)
    assert total_cost(items) == Decimal("0.00195")


def test_sample_openai_shape_prices_to_0_00195() -> None:
    c = call(provider="openai", model="fixture-o")  # total 1000 = 300 + 600 + 100
    items = estimate(c, catalog(SAMPLE_RATES), "pr")
    assert total_cost(items) == Decimal("0.00195")
    assert by_resource(items)[Resource.input_cache_write].line_cost == Decimal("0.00025")


# ------------------------------------------------------------------- basics
def test_line_cost_formula_and_twelve_places() -> None:
    items = estimate(call(uncached=1, read=0, write=0, output=0), catalog(SAMPLE_RATES), "pr")
    (item,) = items
    assert item.line_cost == Decimal("0.000002000000")
    assert item.unit_price == Decimal("2") and item.unit_quantity == M


def test_zero_quantities_produce_no_line_items() -> None:
    items = estimate(call(read=0, write=0), catalog(SAMPLE_RATES), "pr")
    assert {i.resource for i in items} == {Resource.input_uncached, Resource.output}


def test_deterministic_ids_and_costs() -> None:
    a = estimate(call(breakdown={"ephemeral_5m": 100}), catalog(SAMPLE_RATES), "pr_x")
    b = estimate(call(breakdown={"ephemeral_5m": 100}), catalog(SAMPLE_RATES), "pr_x")
    assert [i.model_dump() for i in a] == [i.model_dump() for i in b]
    assert a[0].line_item_id == "li_pr_x_c1_input_uncached"


def test_evidence_class_is_b_for_usage_times_price() -> None:
    items = estimate(call(write=0), catalog(SAMPLE_RATES), "pr")
    assert {i.evidence_class.value for i in items} == {"B"}


# ------------------------------------------------------------ cache writes (V09)
def test_write_total_and_tier_breakdown_are_priced_once() -> None:
    items = estimate(
        call(write=100, breakdown={"ephemeral_5m": 60, "ephemeral_1h": 40}),
        catalog(SAMPLE_RATES),
        "pr",
    )
    got = by_resource(items)
    assert Resource.input_cache_write not in got
    assert got[Resource.input_cache_write_5m].line_cost == Decimal("0.00015")
    assert got[Resource.input_cache_write_1h].line_cost == Decimal("0.00016")
    assert total_cost(items) == Decimal("0.0006") + Decimal("0.0003") + Decimal(
        "0.00031"
    ) + Decimal("0.0008")


def test_write_total_without_tier_stays_unpriced_when_model_has_tiers(V11=None) -> None:
    items = estimate(call(write=100), catalog(SAMPLE_RATES), "pr")
    got = by_resource(items)
    write = got[Resource.input_cache_write]
    assert write.status is LineItemStatus.unpriced
    assert write.unpriced_reason == "cache_write_tier_unknown"
    assert write.quantity == 100
    # the rest is still priced: known subtotal, not a fake total
    assert total_cost(items) == Decimal("0.0006") + Decimal("0.0003") + Decimal("0.0008")


def test_single_tier_write_price_applies_to_untiered_total() -> None:
    items = estimate(
        call(provider="openai", model="fixture-o", write=100), catalog(SAMPLE_RATES), "pr"
    )
    assert by_resource(items)[Resource.input_cache_write].line_cost == Decimal("0.00025")


def test_unknown_tier_label_is_unpriced() -> None:
    items = estimate(call(write=100, breakdown={"ephemeral_2h": 100}), catalog(SAMPLE_RATES), "pr")
    write = by_resource(items)[Resource.input_cache_write]
    assert write.status is LineItemStatus.unpriced
    assert write.unpriced_reason == "cache_write_tier_unsupported"


def test_absent_write_counter_on_write_billed_model_is_a_lower_bound() -> None:
    contracts = [
        {
            "provider": "openai",
            "model_id": "fixture-o",
            "cache_write_tiers": ["input_cache_write"],
            "supported_cache_policies": ["provider_auto"],
            "source_url": "synthetic://test",
            "retrieved_at": "2026-09-26",
        }
    ]
    items = estimate(
        call(provider="openai", model="fixture-o", write=0, notes=["cache_write_field_absent"]),
        catalog(SAMPLE_RATES, cache_contracts=contracts),
        "pr",
    )
    write = by_resource(items)[Resource.input_cache_write]
    assert write.status is LineItemStatus.unpriced
    assert write.unpriced_reason == "cache_write_count_unknown"
    assert write.quantity is None


def test_absent_write_counter_on_free_write_model_is_exact() -> None:
    rates = [r for r in SAMPLE_RATES if r["resource"] != "input_cache_write"]
    items = estimate(
        call(provider="openai", model="fixture-o", write=0, notes=["cache_write_field_absent"]),
        catalog(rates),
        "pr",
    )
    assert all(i.status is LineItemStatus.priced for i in items)


# ----------------------------------------------------------- unknowns (V11, V05)
def test_unknown_model_never_borrows_a_similar_price() -> None:
    items = estimate(call(model="fixture-a-v2"), catalog(SAMPLE_RATES), "pr")
    assert {i.status for i in items} == {LineItemStatus.unpriced}
    assert {i.unpriced_reason for i in items} == {"unknown_model"}
    assert total_cost(items) == 0


def test_alias_resolution_is_explicit() -> None:
    aliases = [
        {
            "provider": "anthropic",
            "alias": "fixture-a-latest",
            "model_id": "fixture-a",
            "source_url": "synthetic://test",
            "retrieved_at": "2026-09-26",
        }
    ]
    items = estimate(
        call(model="fixture-a-latest", write=0), catalog(SAMPLE_RATES, aliases=aliases), "pr"
    )
    assert all(i.status is LineItemStatus.priced for i in items)
    assert items[0].model_id_priced == "fixture-a"


def test_missing_usage_is_unknown_cost_not_zero() -> None:
    c = call(uncached=None, read=None, write=None, output=None, completeness="missing")
    items = estimate(c, catalog(SAMPLE_RATES), "pr")
    assert {i.unpriced_reason for i in items} == {"usage_missing"}
    assert all(i.line_cost is None for i in items)


def test_invalid_usage_is_not_priced() -> None:
    c = call(completeness="invalid")
    items = estimate(c, catalog(SAMPLE_RATES), "pr")
    assert {i.unpriced_reason for i in items} == {"usage_invalid"}


def test_partial_usage_prices_output_only_when_split_is_unknown() -> None:
    c = call(uncached=None, read=None, write=None, output=100, completeness="partial")
    c = c.model_copy(update={"input_total_tokens": 1000})
    items = estimate(c, catalog(SAMPLE_RATES), "pr")
    got = by_resource(items)
    assert got[Resource.input_uncached].status is LineItemStatus.unpriced
    assert got[Resource.input_uncached].unpriced_reason == "input_split_unknown"
    assert got[Resource.input_uncached].quantity == 1000
    assert got[Resource.output].line_cost == Decimal("0.0008")


def test_missing_price_for_one_resource_only_marks_that_resource() -> None:
    rates = [r for r in SAMPLE_RATES if r["resource"] != "output"]
    items = estimate(call(write=0), catalog(rates), "pr")
    got = by_resource(items)
    assert got[Resource.output].unpriced_reason == "price_missing"
    assert got[Resource.input_uncached].status is LineItemStatus.priced


def test_missing_timestamps_cannot_select_a_price_version() -> None:
    items = estimate(call(started=None, ended=None, write=0), catalog(SAMPLE_RATES), "pr")
    assert {i.unpriced_reason for i in items} == {"timestamp_missing"}


# ------------------------------------------------------------- price versions (V12)
def test_price_version_is_chosen_by_call_start_time() -> None:
    rates = [
        record("fixture-a", "input_uncached", "2", end=T_SWITCH, price_id="old"),
        record("fixture-a", "input_uncached", "3", start=T_SWITCH, price_id="new"),
        record("fixture-a", "output", "8"),
    ]
    before = estimate(
        call(read=0, write=0, started=T_SWITCH - 1, ended=T_SWITCH + 5), catalog(rates), "pr"
    )
    after = estimate(
        call(read=0, write=0, started=T_SWITCH, ended=T_SWITCH + 5), catalog(rates), "pr"
    )
    assert by_resource(before)[Resource.input_uncached].price_id == "old"
    assert by_resource(after)[Resource.input_uncached].price_id == "new"
    assert by_resource(after)[Resource.input_uncached].line_cost == Decimal("0.0009")


def test_boundary_call_is_flagged_but_priced_by_start_day() -> None:
    items = estimate(
        call(started=T_CALL - 3000, ended=T_CALL + 2000, write=0), catalog(SAMPLE_RATES), "pr"
    )
    assert all(i.boundary_call for i in items)
    assert all(i.priced_at_ms == T_CALL - 3000 for i in items)


def test_api_family_specific_price_wins_over_generic() -> None:
    rates = [
        record("fixture-a", "input_uncached", "2", price_id="generic"),
        record("fixture-a", "input_uncached", "1", api_family="messages", price_id="messages"),
        record("fixture-a", "output", "8"),
    ]
    items = estimate(call(read=0, write=0, api_family="messages"), catalog(rates), "pr")
    assert by_resource(items)[Resource.input_uncached].price_id == "messages"
    generic = estimate(call(read=0, write=0), catalog(rates), "pr")
    assert by_resource(generic)[Resource.input_uncached].price_id == "generic"


def test_ambiguous_catalog_is_an_error() -> None:
    rates = [
        record("fixture-a", "input_uncached", "2", price_id="one"),
        record("fixture-a", "input_uncached", "3", price_id="two"),
    ]
    with pytest.raises(Exception, match="ambiguous"):
        estimate(call(read=0, write=0, output=0), catalog(rates), "pr")


def test_service_tier_and_region_must_match() -> None:
    items = estimate(
        call(read=0, write=0).model_copy(update={"service_tier": "batch"}),
        catalog(SAMPLE_RATES),
        "pr",
    )
    assert {i.unpriced_reason for i in items} == {"price_missing"}


# ---------------------------------------------------------------- pricing run
def test_pricing_run_is_idempotent_and_replaces_itself(tmp_path) -> None:
    from aiecon.ingest import ingest_paths
    from aiecon.pipeline import fixture_root

    ws = Workspace(tmp_path / "ws")
    ws.init(data_kind=DataKind.synthetic, now_ms=1, aiecon_version=__version__)
    synthetic_catalog = load_synthetic_catalog()
    with Storage.open(ws.db_path) as storage:
        ingest_paths(
            storage,
            [fixture_root() / "events.jsonl"],
            now_ms=2,
            workspace_data_kind=DataKind.synthetic,
        )
        first = run_pricing(storage, "demo-support-v1", synthetic_catalog, now_ms=3)
        second = run_pricing(storage, "demo-support-v1", synthetic_catalog, now_ms=4)
        assert first.manifest.pricing_run_id == second.manifest.pricing_run_id
        assert second.replaced_existing is True
        runs = storage.query("SELECT COUNT(*) FROM meta_runs WHERE run_kind = 'pricing'")[0][0]
        assert runs == 1
        total = storage.query(
            "SELECT SUM(line_cost) FROM current_cost_line_items WHERE status = 'priced'"
        )[0][0]
        assert Decimal(total) == Decimal(first.manifest.known_cost_subtotal_usd)
        assert first.manifest.call_count == 313
        assert first.manifest.unpriced_call_count == 5  # missing usage stays unknown
        assert first.manifest.cost_complete is False
