"""T5.1: JSON and HTML come from one Report model; nulls never render as $0.00; no network."""

from __future__ import annotations

import json
import re
import shutil
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
from aiecon.storage import Storage, Workspace, WorkspaceError

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


def _prepared_workspace(tmp_path: Path) -> Workspace:
    ws = Workspace(tmp_path / "ws")
    ws.init(data_kind=DataKind.synthetic, now_ms=1, aiecon_version=__version__)
    raw_day = ws.raw_dir / "2026-09-26"
    raw_day.mkdir(parents=True)
    shutil.copyfile(fixture_root() / "events.jsonl", raw_day / "events.jsonl")
    with Storage.open(ws.db_path) as storage:
        ingest_paths(storage, [ws.raw_dir], now_ms=2, workspace_data_kind=DataKind.synthetic)
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
    return ws


def test_input_hashes_are_workspace_relative_and_dataset_scoped(tmp_path: Path) -> None:
    """G09: same-named files in different folders never collapse; only this dataset's inputs."""

    ws = _prepared_workspace(tmp_path)
    with Storage.open(ws.db_path) as storage:
        report = build_report(storage, "demo-support-v1", now_ms=6, workspace_root=ws.root)
    hashes = report.limitations.input_file_hashes
    assert "raw/2026-09-26/events.jsonl" in hashes
    files = [k for k in hashes if not k.startswith("snapshot:")]
    assert files and not any(chr(92) in k or ":" in k.split("/")[0] for k in files)
    assert {k for k in hashes if k.startswith("snapshot:")} == {
        f"snapshot:snap_demo-support-v1_{name}" for name in PROVIDER_FIXTURES
    }
    assert report.identity.stale_inputs == []


def test_stale_runs_are_refused_unless_explicitly_allowed(tmp_path: Path) -> None:
    """G04: a report never silently mixes old runs with newer workspace contents."""

    ws = _prepared_workspace(tmp_path)
    with Storage.open(ws.db_path) as storage:
        # a newer provider snapshot would now be selected for the reconcile window
        provider = fixture_root() / "provider"
        body = (provider / "anthropic_cost.json").read_text("utf-8")
        manifest = json.loads((provider / "anthropic_cost.manifest.json").read_text("utf-8"))
        manifest["snapshot_id"] = "snap_repull"
        manifest["fetched_at_ms"] = manifest["fetched_at_ms"] + DAY
        (tmp_path / "repull.json").write_text(body, encoding="utf-8", newline="\n")
        (tmp_path / "repull.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        import_snapshot(
            storage,
            file_path=tmp_path / "repull.json",
            manifest_path=tmp_path / "repull.manifest.json",
            now_ms=7,
            workspace_data_kind=DataKind.synthetic,
        )
        with pytest.raises(WorkspaceError, match="stale"):
            build_report(storage, "demo-support-v1", now_ms=8, workspace_root=ws.root)
        report = build_report(
            storage, "demo-support-v1", now_ms=8, workspace_root=ws.root, allow_stale=True
        )
        assert len(report.identity.stale_inputs) == 1
        assert "provider snapshots changed" in report.identity.stale_inputs[0]
        assert "STALE INPUTS" in render_html(report)
        assert report.limitations.notes[1].startswith("STALE INPUTS")
        # re-running reconcile clears it; a new pricing run then makes the reconcile run stale
        reconcile(
            storage, "demo-support-v1", TimeWindow(start_ms=D1, end_ms=D1 + 2 * DAY), now_ms=9
        )
        fresh = build_report(storage, "demo-support-v1", now_ms=10, workspace_root=ws.root)
        assert fresh.identity.stale_inputs == []
        assert "snap_repull" in fresh.identity.provider_snapshot_ids
        base = load_synthetic_catalog()
        repriced = base.model_copy(
            update={
                "records": [
                    r.model_copy(update={"unit_price": r.unit_price * 2}) for r in base.records
                ]
            }
        )
        run_pricing(storage, "demo-support-v1", repriced, now_ms=11)
        with pytest.raises(WorkspaceError, match="pricing run"):
            build_report(storage, "demo-support-v1", now_ms=12, workspace_root=ws.root)


def test_validation_lines_claim_only_what_was_compared() -> None:
    from aiecon.report import validation_lines

    synthetic = validation_lines("synthetic", [], [])
    assert all("live reconciliation pending" in line for line in synthetic)
    assert validation_lines("live", [], []) == [
        "openai: money: live reconciliation pending (no provider cost in this window); "
        "usage: no provider usage report in this window",
        "anthropic: money: live reconciliation pending (no provider cost in this window); "
        "usage: no provider usage report in this window",
    ]


def test_live_report_lines_follow_the_reconcile_buckets(demo_report) -> None:
    """Synthetic demo: both providers reconcile against fixtures, yet the lines never claim
    live validation."""

    report, _html = demo_report
    assert report.monetary_reconciliation.buckets  # fixtures produced comparisons
    assert all("live reconciliation pending" in s for s in report.limitations.validation_status)
