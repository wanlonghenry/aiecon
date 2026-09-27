"""T3.3 / V17 / V18 / V19 / V30 and the demo billing scenarios."""

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
from aiecon.reconcile import reconcile, shareable_line
from aiecon.spec import ComparisonKind, DataKind, ReconciliationStatus, TimeWindow
from aiecon.storage import Storage, Workspace

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
    day2 = cost_buckets(result)["cost_comparison/anthropic/demo_scope_anthropic/2026-09-27"]
    pct = abs(day2.variance_pct).quantize(Decimal("0.1"))
    where = "(anthropic, demo_scope_anthropic, 2026-09-27 UTC)"
    assert f"Your estimates run {pct}% below provider-reported costs {where}." in lines
    where = "(openai, demo_scope_openai, 2026-09-26 UTC)"
    assert f"Your estimates run 0.0% above provider-reported costs {where}." in lines
    assert not any("invoice" in line for line in lines)
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
