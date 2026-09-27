"""T4.1 / V23 / V24: section 8.3 ROI, TTL segments, already-cached, cross-scope isolation."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from aiecon import __version__
from aiecon.context_econ import analyze, break_even_reuses, segments_for
from aiecon.ingest import ingest_paths
from aiecon.pipeline import fixture_root, load_expected_metrics
from aiecon.pricing import CatalogIndex, load_synthetic_catalog
from aiecon.spec import (
    ApiFamily,
    CallStatus,
    ContextRecommendation,
    DataKind,
    ModelCall,
    PriceCatalog,
    Provider,
    UsageCompleteness,
    UsageFormat,
)
from aiecon.storage import Storage, Workspace

MINUTE = 60_000
T0 = 1_790_467_200_000
FP_A = "a" * 64


def index_with(
    pu: str,
    pw5: str,
    pr: str,
    *,
    pw1: str | None = None,
    policies: tuple[str, ...] = ("ephemeral_5m",),
) -> CatalogIndex:
    def rec(resource: str, price: str) -> dict:
        return {
            "price_id": f"fixture-ctx-{resource}",
            "catalog_version": "ctx-test",
            "provider": "anthropic",
            "model_id": "fixture-ctx",
            "resource": resource,
            "effective_from_ms": 0,
            "unit": "token",
            "unit_price": price,
            "source_url": "synthetic://test",
            "retrieved_at": "2026-09-26",
            "effective_date_basis": "synthetic",
        }

    records = [
        rec("input_uncached", pu),
        rec("input_cache_read", pr),
        rec("input_cache_write_5m", pw5),
        rec("output", "8"),
    ]
    if pw1 is not None:
        records.append(rec("input_cache_write_1h", pw1))
    catalog = PriceCatalog.model_validate(
        {
            "catalog_version": "ctx-test",
            "catalog_kind": "synthetic",
            "description": "context economics test rates",
            "records": records,
            "cache_contracts": [
                {
                    "provider": "anthropic",
                    "model_id": "fixture-ctx",
                    "cache_write_tiers": ["input_cache_write_5m"]
                    + (["input_cache_write_1h"] if pw1 else []),
                    "supported_cache_policies": list(policies),
                    "source_url": "synthetic://test",
                    "retrieved_at": "2026-09-26",
                }
            ],
        }
    )
    return CatalogIndex(catalog)


def call(
    i: int,
    started: int,
    *,
    prefix: int = 10_000,
    fingerprint: str = FP_A,
    scope: str = "scope_a",
    key_id: str = "fpk_one",
    model: str = "fixture-ctx",
    read: int = 0,
    write: int = 0,
    complete: bool = True,
    prefix_tokens: int | None = 10_000,
) -> ModelCall:
    uncached = prefix + 50 - read - write
    return ModelCall(
        dataset_id="d",
        call_id=f"c{i}",
        data_kind=DataKind.synthetic,
        scope_id=scope,
        normalizer_version="0.1.0",
        provider=Provider.anthropic,
        api_family=ApiFamily.messages,
        model_requested=model,
        model_resolved=model,
        status=CallStatus.success,
        started_at_ms=started,
        ended_at_ms=started + 1000,
        usage_format=UsageFormat.anthropic_messages,
        input_total_tokens=uncached + read + write if complete else None,
        input_uncached_tokens=uncached if complete else None,
        input_cache_read_tokens=read if complete else None,
        input_cache_write_tokens=write if complete else None,
        output_tokens=20 if complete else None,
        usage_completeness=UsageCompleteness.complete if complete else UsageCompleteness.missing,
        prefix_fingerprint=fingerprint,
        fingerprint_key_id=key_id,
        prefix_tokens=prefix_tokens,
        prefix_token_count_method="provider_count_tokens",
        cache_policy="none",
    )


def test_section_8_3_rates_single_and_double_access() -> None:
    # Pu=1, Pw=1.25, Pr=0.1 per million tokens; T=10,000 (synthetic test rates only)
    index = index_with("1", "1.25", "0.1")
    single = analyze([call(1, T0)], index, dataset_id="d")
    (group,) = single.groups
    assert group.modeled_savings_usd == Decimal("-0.0025")
    assert group.recommendation is ContextRecommendation.not_beneficial
    double = analyze([call(1, T0), call(2, T0 + MINUTE)], index, dataset_id="d")
    (group,) = double.groups
    assert group.modeled_no_cache_cost_usd == Decimal("0.02")
    assert group.modeled_cache_cost_usd == Decimal("0.0135")
    assert group.modeled_savings_usd == Decimal("0.0065")
    assert group.recommendation is ContextRecommendation.beneficial
    assert group.break_even_reuses == 2
    assert break_even_reuses(Decimal("1"), Decimal("1.25"), Decimal("0.1")) == 2
    assert break_even_reuses(Decimal("0.1"), Decimal("1.25"), Decimal("0.1")) is None


def test_ttl_expiry_creates_new_write_segments_and_longer_ttl_can_win() -> None:
    calls = [call(i, T0 + i * 8 * MINUTE) for i in range(3)]
    only_5m = analyze(calls, index_with("2", "2.5", "0.5"), dataset_id="d")
    (group,) = only_5m.groups
    assert len(group.segments) == 3  # every call is a cold write under a 5-minute TTL
    assert group.modeled_savings_usd < 0
    assert group.recommendation is ContextRecommendation.not_beneficial
    both = analyze(
        calls,
        index_with("2", "2.5", "0.5", pw1="4", policies=("ephemeral_5m", "ephemeral_1h")),
        dataset_id="d",
    )
    (group,) = both.groups
    assert group.scenario_policy == "ephemeral_1h"
    assert len(group.segments) == 1
    # 3 x 10k x $2/M = 0.06 uncached; one 1h write 10k x $4/M + two reads 10k x $0.5/M = 0.05
    assert group.modeled_savings_usd == Decimal("0.01")
    assert group.supported_ttl_candidates == ["ephemeral_5m", "ephemeral_1h"]
    assert segments_for(calls, 5 * MINUTE)[1].first_call_id == "c1"


def test_already_cached_prefix_is_not_counted_as_savings() -> None:
    calls = [call(0, T0), call(1, T0 + MINUTE, read=10_000), call(2, T0 + 2 * MINUTE, read=10_000)]
    (group,) = analyze(calls, index_with("2", "2.5", "0.5"), dataset_id="d").groups
    assert group.recommendation is ContextRecommendation.already_cached
    assert group.modeled_savings_usd is None
    assert group.observed_cache_read_tokens == 20_000
    assert group.candidate_repeated_prefix_tokens == 0


def test_groups_never_merge_across_scope_key_or_model(V23: None = None) -> None:
    calls = [
        call(0, T0),
        call(1, T0 + MINUTE, scope="scope_b"),
        call(2, T0 + 2 * MINUTE, key_id="fpk_two"),
        call(3, T0 + 3 * MINUTE, model="fixture-other"),
        call(4, T0 + 4 * MINUTE),
    ]
    result = analyze(calls, index_with("2", "2.5", "0.5"), dataset_id="d")
    assert len(result.groups) == 4
    main = next(
        g
        for g in result.groups
        if g.scope_id == "scope_a"
        and g.fingerprint_key_id == "fpk_one"
        and g.model_resolved == "fixture-ctx"
    )
    assert main.calls == 2 and main.recommendation is ContextRecommendation.beneficial
    other_model = next(g for g in result.groups if g.model_resolved == "fixture-other")
    assert other_model.recommendation is ContextRecommendation.insufficient_evidence


def test_missing_prefix_tokens_or_usage_is_insufficient_evidence() -> None:
    index = index_with("2", "2.5", "0.5")
    (no_tokens,) = analyze(
        [call(0, T0, prefix_tokens=None), call(1, T0 + 1000, prefix_tokens=None)],
        index,
        dataset_id="d",
    ).groups
    assert no_tokens.recommendation is ContextRecommendation.insufficient_evidence
    assert no_tokens.modeled_savings_usd is None
    (no_usage,) = analyze([call(0, T0, complete=False)], index, dataset_id="d").groups
    assert no_usage.recommendation is ContextRecommendation.insufficient_evidence
    plain = analyze(
        [call(0, T0).model_copy(update={"prefix_fingerprint": None})], index, dataset_id="d"
    )
    assert plain.groups == [] and plain.calls_without_prefix_evidence == 1


def test_demo_groups_match_expected_metrics(tmp_path: Path) -> None:
    expected = load_expected_metrics()["context_economics"]
    ws = Workspace(tmp_path / "ws")
    ws.init(data_kind=DataKind.synthetic, now_ms=1, aiecon_version=__version__)
    with Storage.open(ws.db_path) as storage:
        ingest_paths(
            storage,
            [fixture_root() / "events.jsonl"],
            now_ms=2,
            workspace_data_kind=DataKind.synthetic,
        )
        calls = storage.list_calls("demo-support-v1")
    result = analyze(calls, CatalogIndex(load_synthetic_catalog()), dataset_id="demo-support-v1")
    by_fp = {g.prefix_fingerprint: g for g in result.groups}
    assert len(by_fp) == 4
    a = by_fp["a" * 64]
    assert a.calls == expected["group_a_uncached_repeat"]["appearances"]
    assert a.recommendation is ContextRecommendation.beneficial
    assert a.scenario_policy == "ephemeral_5m" and len(a.segments) == 1
    assert a.modeled_savings_usd == Decimal(
        expected["group_a_uncached_repeat"]["ephemeral_5m"]["savings_usd"]
    )
    assert a.break_even_reuses == expected["group_a_uncached_repeat"]["break_even_reuses"]
    b = by_fp["b" * 64]
    assert b.recommendation is ContextRecommendation.already_cached
    assert (
        b.observed_cache_read_tokens
        == expected["group_b_already_cached"]["observed_cache_read_tokens"]
    )
    c = by_fp["c" * 64]
    assert c.recommendation is ContextRecommendation.beneficial
    assert c.scenario_policy == expected["group_c_cross_ttl"]["expected_policy"]
    assert c.modeled_savings_usd == Decimal(
        expected["group_c_cross_ttl"]["ephemeral_1h"]["savings_usd"]
    )
    d = by_fp["d" * 64]
    assert d.recommendation is ContextRecommendation.not_beneficial
    assert d.modeled_savings_usd == Decimal(
        expected["group_d_single_call"]["ephemeral_5m"]["savings_usd"]
    )
    assert result.calls_with_prefix_evidence == 82
    assert result.calls_without_prefix_evidence == 313 - 82
