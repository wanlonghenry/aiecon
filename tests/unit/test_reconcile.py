"""T3.3 / V17 / V18 / V19 / V30, the demo billing scenarios and the gap-review regressions."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from aiecon import __version__
from aiecon.billing.importer import import_snapshot
from aiecon.ingest import ingest_paths
from aiecon.pipeline import PROVIDER_FIXTURES, fixture_root, load_expected_metrics
from aiecon.pricing import load_synthetic_catalog
from aiecon.pricing.run import run_pricing
from aiecon.reconcile import (
    _tier_adjustments,
    _TierItem,
    reconcile,
    resolve_unknown_write_tier,
    shareable_line,
)
from aiecon.spec import (
    CallStatus,
    ComparisonKind,
    DataKind,
    ModelCall,
    Provider,
    ReconciliationStatus,
    TimeWindow,
    UsageCompleteness,
)
from aiecon.storage import Storage, Workspace, WorkspaceError

D1 = 1_790_380_800_000
DAY = 86_400_000
WINDOW = TimeWindow(start_ms=D1, end_ms=D1 + 2 * DAY)


@pytest.fixture
def demo_storage(tmp_path: Path):
    ws = Workspace(tmp_path / "ws")
    ws.init(data_kind=DataKind.synthetic, now_ms=1, aiecon_version=__version__)
    storage = Storage.open(ws.db_path)
    ingest_paths(
        storage, [fixture_root() / "events.jsonl"], now_ms=2, workspace_data_kind=DataKind.synthetic
    )
    run_pricing(storage, "demo-support-v1", load_synthetic_catalog(), now_ms=3)
    root = fixture_root() / "provider"
    for name in PROVIDER_FIXTURES:
        import_snapshot(
            storage,
            file_path=root / f"{name}.json",
            manifest_path=root / f"{name}.manifest.json",
            now_ms=4,
            workspace_data_kind=DataKind.synthetic,
        )
    yield storage
    storage.close()


def cost_buckets(result):
    return {
        b.bucket_key: b
        for b in result.buckets
        if b.comparison_kind is ComparisonKind.cost_comparison
    }


def test_demo_produces_matched_explained_and_unexplained_buckets(demo_storage: Storage) -> None:
    expected = load_expected_metrics()
    result = reconcile(demo_storage, "demo-support-v1", WINDOW, now_ms=5)
    costs = cost_buckets(result)
    assert len(costs) == 4  # two providers x two UTC days

    for day in ("2026-09-26", "2026-09-27"):
        openai = costs[f"cost_comparison/openai/demo_scope_openai/{day}"]
        assert openai.status is ReconciliationStatus.matched
        assert openai.signed_variance_usd == 0
        assert openai.local_estimate_usd == Decimal(
            expected["estimated_cost_by_provider_day_usd"][f"openai/{day}"]
        )
        assert openai.provider_cost_usd == Decimal(expected["provider_cost_usd"]["openai"][day])

    day1 = costs["cost_comparison/anthropic/demo_scope_anthropic/2026-09-26"]
    assert day1.status is ReconciliationStatus.variance
    assert day1.signed_variance_usd == -Decimal(expected["phantom_call_cost_usd"])
    assert [a.code for a in day1.explained_adjustments] == ["capture_gap"]
    assert day1.explained_adjustments[0].evidence_backed is True
    assert day1.unexplained_delta_usd == 0
    assert "explained" in day1.reasons and "unpriced_calls" in day1.reasons
    # the algebra holds: signed - explained == unexplained
    assert (
        day1.signed_variance_usd - day1.explained_adjustments[0].signed_amount_usd
        == day1.unexplained_delta_usd
    )

    day2 = costs["cost_comparison/anthropic/demo_scope_anthropic/2026-09-27"]
    assert day2.status is ReconciliationStatus.variance
    assert day2.signed_variance_usd == -Decimal(expected["unexplained_residue_usd"])
    assert day2.explained_adjustments == []
    assert day2.unexplained_delta_usd == -Decimal(expected["unexplained_residue_usd"])
    assert "unexplained" in day2.reasons
    # the boundary call straddles midnight and is surfaced as a hypothesis, never as evidence
    boundary = [b for b in costs.values() if any(h.code == "boundary_call" for h in b.hypotheses)]
    assert len(boundary) == 1 and boundary[0].dimensions["day"] == "2026-09-26"

    # usage comparisons are per model and separate from money
    usage = [b for b in result.buckets if b.comparison_kind is ComparisonKind.usage_comparison]
    assert len(usage) == 4
    anthropic_day1 = next(
        b for b in usage if b.provider.value == "anthropic" and b.dimensions["day"] == "2026-09-26"
    )
    assert anthropic_day1.status is ReconciliationStatus.variance
    assert anthropic_day1.usage_variance["input_uncached_tokens"] == -20000
    assert anthropic_day1.usage_variance["output_tokens"] == -4000
    assert "capture_gap" in anthropic_day1.reasons
    openai_day2 = next(
        b for b in usage if b.provider.value == "openai" and b.dimensions["day"] == "2026-09-27"
    )
    assert openai_day2.status is ReconciliationStatus.matched
    assert openai_day2.usage_variance["requests"] == 0

    # run bookkeeping
    assert result.manifest.bucket_count == 8
    assert result.manifest.status_counts["cost_comparison:matched"] == 2
    assert (
        demo_storage.active_run("reconcile", "demo-support-v1")[0]
        == result.manifest.reconcile_run_id
    )
    again = reconcile(demo_storage, "demo-support-v1", WINDOW, now_ms=6)
    assert again.manifest.reconcile_run_id == result.manifest.reconcile_run_id  # deterministic
    assert demo_storage.query("SELECT COUNT(*) FROM reconciliation_buckets")[0][0] == 8


def test_shareable_lines_only_for_positive_provider_cost(demo_storage: Storage) -> None:
    result = reconcile(demo_storage, "demo-support-v1", WINDOW, now_ms=5)
    lines = result.summary_lines
    assert len(lines) == 4
    costs = cost_buckets(result)
    day2 = costs["cost_comparison/anthropic/demo_scope_anthropic/2026-09-27"]
    pct = abs(day2.variance_pct).quantize(Decimal("0.1"))
    where = "(anthropic, demo_scope_anthropic, 2026-09-27 UTC)"
    assert any(
        line.startswith(f"Your estimates run {pct}% below provider-reported costs {where}.")
        for line in lines
    )
    where = "(openai, demo_scope_openai, 2026-09-26 UTC)"
    assert any(
        line.startswith(f"Your estimates run 0.0% above provider-reported costs {where}.")
        for line in lines
    )
    assert not any("invoice" in line for line in lines)
    # G08: a known-cost lower bound and provisional provider data are said out loud
    day1 = costs["cost_comparison/anthropic/demo_scope_anthropic/2026-09-26"]
    assert day1.local_cost_complete is False
    line = shareable_line(day1)
    assert f"known-cost lower bound: {day1.local_unpriced_call_count} call(s)" in line
    assert "provider data provisional" in line
    complete = day1.model_copy(update={"local_cost_complete": True, "reasons": []})
    assert "[" not in shareable_line(complete)
    # V18 / V30: B = 0, B < 0 and B missing never print a percentage
    zero = day2.model_copy(update={"provider_cost_usd": Decimal(0), "variance_pct": None})
    assert "%" not in shareable_line(zero)
    negative = day2.model_copy(update={"provider_cost_usd": Decimal("-1"), "variance_pct": None})
    assert "%" not in shareable_line(negative)
    missing = day2.model_copy(
        update={
            "provider_cost_usd": None,
            "variance_pct": None,
            "status": ReconciliationStatus.no_provider_cost,
        }
    )
    assert shareable_line(missing).startswith("no_provider_cost — live reconciliation pending")


def test_no_provider_data_and_scope_mismatch(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    ws.init(data_kind=DataKind.synthetic, now_ms=1, aiecon_version=__version__)
    with Storage.open(ws.db_path) as storage:
        ingest_paths(
            storage,
            [fixture_root() / "events.jsonl"],
            now_ms=2,
            workspace_data_kind=DataKind.synthetic,
        )
        run_pricing(storage, "demo-support-v1", load_synthetic_catalog(), now_ms=3)
        result = reconcile(storage, "demo-support-v1", WINDOW, now_ms=4)
        costs = cost_buckets(result)
        assert {b.status for b in costs.values()} == {ReconciliationStatus.no_provider_cost}
        assert all(b.provider_cost_usd is None and b.variance_pct is None for b in costs.values())
        assert all(line.startswith("no_provider_cost") for line in result.summary_lines)

        # an organisation-wide snapshot under a different scope must not be compared (V17)
        records = [
            {
                "record_id": "org_day1",
                "window_start_ms": D1,
                "window_end_ms": D1 + DAY,
                "dimensions_json": {"project_id": "whole_org", "line_item": "all"},
                "amount_original": "99",
                "amount_unit": "usd",
                "currency": "USD",
                "usage_json": None,
            }
        ]
        body = json.dumps({"records": records}) + "\n"
        (tmp_path / "org.json").write_text(body, encoding="utf-8", newline="\n")
        manifest = {
            "schema_version": "0.1",
            "snapshot_id": "snap_org",
            "provider": "openai",
            "scope_id": "whole_org",
            "record_kind": "provider_cost",
            "data_kind": "synthetic",
            "grain": "1d/line_item,project_id",
            "query_window": {"start_ms": D1, "end_ms": D1 + DAY},
            "fetched_at_ms": D1 + 3 * DAY,
            "source_ref": "synthetic://test/org-wide",
            "source_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "finality": "provisional",
            "snapshot_complete": True,
        }
        (tmp_path / "org.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        import_snapshot(
            storage,
            file_path=tmp_path / "org.json",
            manifest_path=tmp_path / "org.manifest.json",
            now_ms=5,
            workspace_data_kind=DataKind.synthetic,
        )
        result = reconcile(storage, "demo-support-v1", WINDOW, now_ms=6)
        org = cost_buckets(result)["cost_comparison/openai/whole_org/2026-09-26"]
        assert org.status is ReconciliationStatus.scope_mismatch
        assert org.variance_pct is None and org.local_call_count == 0
        local = cost_buckets(result)["cost_comparison/openai/demo_scope_openai/2026-09-26"]
        assert local.status is ReconciliationStatus.scope_mismatch


def test_variance_pct_null_when_b_is_zero_or_negative(demo_storage: Storage) -> None:
    from aiecon.reconcile import _variance_pct

    assert _variance_pct(Decimal("1"), Decimal("0")) is None
    assert _variance_pct(Decimal("1"), Decimal("-2")) is None
    assert _variance_pct(Decimal("1.1"), Decimal("1")) == Decimal("10.0000")


# ------------------------------------------------------------ gap-review regressions
ANTHROPIC_DAY1_USAGE = {
    "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 2500},
    "cache_read_input_tokens": 35000,
    "output_tokens": 29225,
    "uncached_input_tokens": 184750,
}
NO_USAGE = {
    "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0},
    "cache_read_input_tokens": 0,
    "output_tokens": 100,
    "uncached_input_tokens": 1000,
}


def import_rows(
    storage: Storage,
    tmp_path: Path,
    *,
    name: str,
    record_kind: str,
    grain: str,
    rows: list[dict],
    window: tuple[int, int],
    fetched_at: int,
    dedicated: bool | None,
) -> None:
    body = json.dumps({"records": rows}, indent=2, sort_keys=True) + "\n"
    (tmp_path / f"{name}.json").write_text(body, encoding="utf-8", newline="\n")
    manifest = {
        "schema_version": "0.1",
        "snapshot_id": name,
        "provider": "anthropic",
        "scope_id": "demo_scope_anthropic",
        "record_kind": record_kind,
        "data_kind": "synthetic",
        "grain": grain,
        "query_window": {"start_ms": window[0], "end_ms": window[1]},
        "fetched_at_ms": fetched_at,
        "source_ref": f"synthetic://test/{name}",
        "source_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "finality": "provisional",
        "snapshot_complete": True,
        "scope_dedicated": dedicated,
    }
    (tmp_path / f"{name}.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    import_snapshot(
        storage,
        file_path=tmp_path / f"{name}.json",
        manifest_path=tmp_path / f"{name}.manifest.json",
        now_ms=fetched_at,
        workspace_data_kind=DataKind.synthetic,
    )


def usage_row(record_id: str, day_ms: int, model: str, usage: dict) -> dict:
    return {
        "record_id": record_id,
        "window_start_ms": day_ms,
        "window_end_ms": day_ms + DAY,
        "dimensions_json": {"model": model, "workspace_id": "demo_scope_anthropic"},
        "amount_original": None,
        "amount_unit": None,
        "currency": None,
        "usage_json": usage,
    }


def anthropic_usage_day1(result):
    return {
        b.dimensions["model"]: b
        for b in result.buckets
        if b.comparison_kind is ComparisonKind.usage_comparison
        and b.provider.value == "anthropic"
        and b.dimensions["day"] == "2026-09-26"
    }


def test_daily_grains_require_whole_utc_day_windows(demo_storage: Storage) -> None:
    """G08: partial days are never compared with daily provider totals."""

    partial = TimeWindow(start_ms=D1 + 3_600_000, end_ms=D1 + 2 * DAY)
    with pytest.raises(WorkspaceError, match="whole UTC days"):
        reconcile(demo_storage, "demo-support-v1", partial, now_ms=5)


def test_unknown_cost_calls_never_match_by_tolerance(demo_storage: Storage, tmp_path: Path) -> None:
    """G06: a lower-bound estimate inside the tolerance band is ``unpriced``, not ``matched``."""

    first = reconcile(demo_storage, "demo-support-v1", WINDOW, now_ms=5)
    target = next(
        b
        for b in cost_buckets(first).values()
        if b.provider.value == "anthropic" and b.local_unpriced_call_count > 0
    )
    day_ms = target.window_start_ms
    other_day = next(
        b
        for b in cost_buckets(first).values()
        if b.provider.value == "anthropic" and b.window_start_ms != day_ms
    )
    import_rows(
        demo_storage,
        tmp_path,
        name="snap_near_match",
        record_kind="provider_cost",
        grain="1d/workspace_id",
        rows=[
            {
                "record_id": "near",
                "window_start_ms": day_ms,
                "window_end_ms": day_ms + DAY,
                "dimensions_json": {"workspace_id": "demo_scope_anthropic"},
                "amount_original": str(target.local_estimate_usd + Decimal("0.005")),
                "amount_unit": "usd",
                "currency": "USD",
                "usage_json": None,
            }
        ],
        window=(day_ms, day_ms + DAY),
        fetched_at=D1 + 5 * DAY,
        dedicated=True,
    )
    second = reconcile(demo_storage, "demo-support-v1", WINDOW, now_ms=6)
    bucket = cost_buckets(second)[target.bucket_key]
    assert bucket.provider_snapshot_ids == ["snap_near_match"]
    assert bucket.absolute_variance_usd <= bucket.tolerance_usd
    assert bucket.status is ReconciliationStatus.unpriced
    assert bucket.local_cost_complete is False and "unpriced_calls" in bucket.reasons
    line = shareable_line(bucket)
    assert "%" not in line and line.startswith("unpriced")
    # the other day is still supplied by the fixture snapshot: one-day re-pulls replace one day
    untouched = cost_buckets(second)[other_day.bucket_key]
    assert untouched.provider_snapshot_ids == other_day.provider_snapshot_ids
    assert untouched.provider_cost_usd == other_day.provider_cost_usd
    assert second.manifest.reconcile_run_id != first.manifest.reconcile_run_id


def test_capture_gap_uses_each_models_own_prices(demo_storage: Storage, tmp_path: Path) -> None:
    """G05: surplus on a model without local prices is never priced with another model's rates."""

    expected = load_expected_metrics()
    import_rows(
        demo_storage,
        tmp_path,
        name="snap_two_models",
        record_kind="provider_usage",
        grain="1d/model,workspace_id",
        rows=[
            usage_row("v1", D1, "fixture-anthropic-v1", ANTHROPIC_DAY1_USAGE),
            usage_row("v2", D1, "fixture-anthropic-v2", NO_USAGE),
        ],
        window=(D1, D1 + DAY),
        fetched_at=D1 + 5 * DAY,
        dedicated=True,
    )
    result = reconcile(demo_storage, "demo-support-v1", WINDOW, now_ms=6)
    usage = anthropic_usage_day1(result)
    assert set(usage) == {"fixture-anthropic-v1", "fixture-anthropic-v2"}
    assert usage["fixture-anthropic-v2"].local_call_count == 0
    assert usage["fixture-anthropic-v2"].usage_variance["input_uncached_tokens"] == -1000
    day1 = cost_buckets(result)["cost_comparison/anthropic/demo_scope_anthropic/2026-09-26"]
    # the v1 surplus is priced with v1's own rates; the v2 surplus has no local price, so the
    # whole gap stays a hypothesis instead of evidence and the v2 tokens are not priced at all
    assert day1.explained_adjustments == []
    gap = next(h for h in day1.hypotheses if h.code == "capture_gap")
    assert gap.signed_amount_usd == -Decimal(expected["phantom_call_cost_usd"])
    assert "fixture-anthropic-v2" not in gap.description
    assert "no usable price" in gap.description
    assert day1.unexplained_delta_usd == day1.signed_variance_usd


def test_surplus_in_non_dedicated_scope_is_a_hypothesis(
    demo_storage: Storage, tmp_path: Path
) -> None:
    """G05: without a dedicated scope a provider-side surplus may be other traffic."""

    expected = load_expected_metrics()
    import_rows(
        demo_storage,
        tmp_path,
        name="snap_shared_scope",
        record_kind="provider_usage",
        grain="1d/model,workspace_id",
        rows=[usage_row("v1", D1, "fixture-anthropic-v1", ANTHROPIC_DAY1_USAGE)],
        window=(D1, D1 + DAY),
        fetched_at=D1 + 5 * DAY,
        dedicated=None,
    )
    result = reconcile(demo_storage, "demo-support-v1", WINDOW, now_ms=6)
    day1 = cost_buckets(result)["cost_comparison/anthropic/demo_scope_anthropic/2026-09-26"]
    assert day1.explained_adjustments == []
    gap = next(h for h in day1.hypotheses if h.code == "capture_gap")
    assert gap.evidence_backed is False
    assert gap.signed_amount_usd == -Decimal(expected["phantom_call_cost_usd"])
    assert "other_traffic_possible" in day1.reasons and "unexplained" in day1.reasons
    usage_day1 = anthropic_usage_day1(result)["fixture-anthropic-v1"]
    assert "provider_exceeds_local" in usage_day1.reasons
    assert "capture_gap" not in usage_day1.reasons
    # the cost snapshot still declares a dedicated scope, but the usage evidence does not:
    # the day counts as dedicated only when every snapshot that feeds it says so
    assert day1.status is ReconciliationStatus.variance


# ------------------------------------------------------ cache-tier resolution (live)
def _call_with_write(call_id: str, write: int, breakdown: dict | None) -> ModelCall:
    return ModelCall(
        dataset_id="d",
        call_id=call_id,
        data_kind=DataKind.live,
        scope_id="s",
        normalizer_version="0.1.0",
        provider=Provider.anthropic,
        model_requested="claude-haiku-4-5-20251001",
        status=CallStatus.success,
        started_at_ms=D1,
        ended_at_ms=D1 + 1000,
        input_total_tokens=write + 17,
        input_uncached_tokens=17,
        input_cache_read_tokens=0,
        input_cache_write_tokens=write,
        output_tokens=5,
        cache_write_breakdown=breakdown,
        usage_completeness=UsageCompleteness.complete,
    )


def test_unknown_write_tier_is_resolved_from_provider_tier_totals() -> None:
    """Seen live: one write logged without a tier; the Console usage export shows 1h = 0."""

    known = _call_with_write("c_known", 30812, {"ephemeral_5m": 30812, "ephemeral_1h": 0})
    unknown = _call_with_write("c_unknown", 30812, None)
    provider = {"input_cache_write_5m_tokens": 61624, "input_cache_write_1h_tokens": 0}
    assert resolve_unknown_write_tier([known, unknown], provider) == (
        "ephemeral_5m",
        30812,
        ["c_unknown"],
    )
    provider_1h = {"input_cache_write_5m_tokens": 30812, "input_cache_write_1h_tokens": 30812}
    assert resolve_unknown_write_tier([known, unknown], provider_1h)[0] == "ephemeral_1h"
    # ambiguous or contradicting totals resolve nothing
    mixed = {"input_cache_write_5m_tokens": 46218, "input_cache_write_1h_tokens": 15406}
    assert resolve_unknown_write_tier([known, unknown], mixed)[0] is None
    assert resolve_unknown_write_tier([known, unknown], {})[0] is None
    assert resolve_unknown_write_tier([known], provider) == (None, 0, [])


def test_tier_adjustments_are_evidence_only_when_resolved() -> None:
    price = Decimal("0.00000125")
    resolved = _TierItem("claude-haiku-4-5-20251001", "ephemeral_5m", 30812, price, ["r1"], ["c1"])
    explained, hypotheses, reasons = _tier_adjustments([resolved])
    assert [a.code for a in explained] == ["cache_tier"] and hypotheses == []
    assert explained[0].evidence_backed is True and explained[0].evidence_refs == ["r1", "c1"]
    assert explained[0].signed_amount_usd == -Decimal("0.038515")
    assert reasons == ["cache_tier"]
    unresolved = _TierItem("claude-haiku-4-5-20251001", None, 30812, price, ["r1"], ["c1"])
    explained, hypotheses, reasons = _tier_adjustments([unresolved])
    assert explained == [] and hypotheses[0].evidence_backed is False
    assert reasons == ["cache_write_tier_unknown"]
    unpriced = _TierItem("m", None, 10, None, [], ["c1"])
    assert _tier_adjustments([unpriced]) == ([], [], ["cache_write_tier_unknown"])
