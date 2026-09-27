"""T5.1: JSON and HTML come from one Report model; nulls never render as $0.00; no network."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from aiecon import __version__
from aiecon.billing.importer import import_snapshot
from aiecon.ingest import ingest_paths
from aiecon.pipeline import PROVIDER_FIXTURES, fixture_root, load_expected_metrics
from aiecon.pricing import load_synthetic_catalog
from aiecon.pricing.run import run_pricing
from aiecon.reconcile import reconcile
from aiecon.report import build_report, render_html, write_report
from aiecon.spec import DataKind, TimeWindow
from aiecon.spec.report import Report
from aiecon.storage import Storage, Workspace

D1 = 1_790_380_800_000
DAY = 86_400_000


@pytest.fixture(scope="module")
def demo_report(tmp_path_factory) -> tuple[Report, Path]:
    tmp = tmp_path_factory.mktemp("report")
    ws = Workspace(tmp / "ws")
    ws.init(data_kind=DataKind.synthetic, now_ms=1, aiecon_version=__version__)
    with Storage.open(ws.db_path) as storage:
        ingest_paths(
            storage,
            [fixture_root() / "events.jsonl"],
            now_ms=2,
            workspace_data_kind=DataKind.synthetic,
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
        reconcile(
            storage, "demo-support-v1", TimeWindow(start_ms=D1, end_ms=D1 + 2 * DAY), now_ms=5
        )
        report = build_report(
            storage, "demo-support-v1", now_ms=6, workspace_id="ws_demo", monthly_requests=10_000
        )
        paths, _manifest = write_report(
            report, tmp / "out" / "report.html", now_ms=6, catalog_hash="abc"
        )
    return report, paths.html


def test_json_and_html_share_one_model(demo_report) -> None:
    report, html_path = demo_report
    expected = load_expected_metrics()
    data = json.loads(html_path.with_suffix(".json").read_text("utf-8"))
    html = html_path.read_text("utf-8")
    cpso = data["outcome_economics"]["cost_per_successful_outcome_usd"]
    assert Decimal(cpso) == Decimal(expected["cost_per_successful_outcome_usd"])
    assert f"${Decimal(cpso):.6f}" in html
    assert data["outcome_economics"]["is_lower_bound"] is True
    assert "known-cost lower bound" in html
    assert data["identity"]["data_kind"] == "synthetic"
    assert "SYNTHETIC DEMO" in html
    total = data["monetary_reconciliation"]["total_local_estimate_usd"]
    assert f"${Decimal(total):.6f}" in html
    assert data["opportunities"]["joint_savings"] == "not computed"
    assert "not computed" in html
    assert Decimal(data["opportunities"]["best_single_action_savings_usd"]) == Decimal(
        expected["context_economics"]["group_a_uncached_repeat"]["ephemeral_5m"]["savings_usd"]
    )
    assert len(data["waste_findings"]["findings"]) == 10 + 10 + 3 + 2
    assert data["context_economics"]["projections"][0]["requests_per_month"] == 10000
    assert "projected scenario" in html.lower() or "Projected monthly scenario" in html
    manifest = json.loads(html_path.with_name("report-manifest.json").read_text("utf-8"))
    assert manifest["pricing_run_id"] == data["identity"]["pricing_run_id"]
    assert manifest["report_json_sha256"] and manifest["report_html_sha256"]
    assert set(manifest["provider_snapshot_ids"]) == set(data["identity"]["provider_snapshot_ids"])


def test_nulls_render_as_unknown_never_as_zero_dollars(demo_report) -> None:
    report, _ = demo_report
    stripped = report.model_copy(
        update={
            "outcome_economics": report.outcome_economics.model_copy(
                update={"cost_per_successful_outcome_usd": None, "note": "no successful outcomes"}
            ),
            "opportunities": report.opportunities.model_copy(
                update={
                    "best_single_action_savings_usd": None,
                    "best_single_action_finding_id": None,
                }
            ),
        }
    )
    html = render_html(stripped)
    assert "no successful outcomes" in html
    assert "no evidence-complete positive scenario" in html
    # the two removed values are shown as Unknown, and no synthetic zero appears for them
    assert html.count("Unknown") >= 2
    # every finding with a null saving must say Unknown, never $0.00
    for f in report.waste_findings.findings:
        if f.modeled_savings_usd is None:
            assert "savings Unknown" in html


def test_html_is_self_contained_and_hides_secrets(demo_report) -> None:
    _, html_path = demo_report
    html = html_path.read_text("utf-8")
    assert "<script" not in html
    assert "<link" not in html
    assert not re.search(r"""(src|href)=["']https?://""", html)
    assert "url(" not in html
    assert "sk-" not in html and "Traceback" not in html
    assert 'name="viewport"' in html


def test_report_json_validates_against_model(demo_report) -> None:
    _, html_path = demo_report
    text = html_path.with_suffix(".json").read_text("utf-8")
    again = Report.model_validate_json(text)
    assert again.identity.call_count == 313
    assert again.monetary_reconciliation.ran is True
    assert len(again.monetary_reconciliation.buckets) == 4
    assert len(again.usage_coverage.buckets) == 4
    # decimals stay strings in JSON
    raw = json.loads(text)
    assert isinstance(raw["outcome_economics"]["cohort_known_cost_usd"], str)
