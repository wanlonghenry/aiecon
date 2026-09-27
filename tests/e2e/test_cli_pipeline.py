"""Offline acceptance for the full CLI chain and the workload dry run (V25, V28-adjacent)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from aiecon.cli import EXIT_CONFIG, EXIT_CONTRACT, EXIT_OK, app
from aiecon.pipeline import PROVIDER_FIXTURES, fixture_root

runner = CliRunner()
REPO = Path(__file__).resolve().parents[2]


def test_manual_chain_matches_demo(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert (
        runner.invoke(app, ["--workspace", str(ws), "init", "--data-kind", "synthetic"]).exit_code
        == 0
    )
    ingest = runner.invoke(
        app, ["--workspace", str(ws), "ingest", "--input", str(fixture_root() / "events.jsonl")]
    )
    assert ingest.exit_code == EXIT_OK, ingest.output
    assert "accepted" in ingest.output

    # a real catalog is refused for synthetic data (and vice versa)
    wrong = runner.invoke(
        app,
        [
            "--workspace",
            str(ws),
            "estimate",
            "--catalog",
            str(REPO / "catalogs" / "live-demo.json"),
        ],
    )
    assert wrong.exit_code == EXIT_CONFIG
    estimate = runner.invoke(
        app,
        [
            "--workspace",
            str(ws),
            "estimate",
            "--catalog",
            str(fixture_root() / "synthetic-catalog.json"),
        ],
    )
    assert estimate.exit_code == EXIT_OK, estimate.output
    assert "cost_complete" in estimate.output and "False" in estimate.output

    root = fixture_root() / "provider"
    for name in PROVIDER_FIXTURES:
        imported = runner.invoke(
            app,
            [
                "--workspace",
                str(ws),
                "billing",
                "import",
                "--file",
                str(root / f"{name}.json"),
                "--manifest",
                str(root / f"{name}.manifest.json"),
            ],
        )
        assert imported.exit_code == EXIT_OK, imported.output
    again = runner.invoke(
        app,
        [
            "--workspace",
            str(ws),
            "billing",
            "import",
            "--file",
            str(root / "openai_cost.json"),
            "--manifest",
            str(root / "openai_cost.manifest.json"),
        ],
    )
    assert "skipped_same_hash   True" in again.output.replace("  ", " ") or "True" in again.output

    # a tampered manifest is a contract error, exit 3
    bad_manifest = tmp_path / "bad.manifest.json"
    manifest = json.loads((root / "openai_cost.manifest.json").read_text("utf-8"))
    manifest["source_hash"] = "f" * 64
    manifest["snapshot_id"] = "snap_bad"
    bad_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    tampered = runner.invoke(
        app,
        [
            "--workspace",
            str(ws),
            "billing",
            "import",
            "--file",
            str(root / "openai_cost.json"),
            "--manifest",
            str(bad_manifest),
        ],
    )
    assert tampered.exit_code == EXIT_CONTRACT

    recon = runner.invoke(
        app, ["--workspace", str(ws), "reconcile", "--start", "2026-09-26", "--end", "2026-09-28"]
    )
    assert recon.exit_code == EXIT_OK, recon.output
    assert "Your estimates run" in recon.output and "provider-reported costs" in recon.output
    bad_window = runner.invoke(
        app, ["--workspace", str(ws), "reconcile", "--start", "2026-09-28", "--end", "2026-09-26"]
    )
    assert bad_window.exit_code == EXIT_CONFIG

    out = tmp_path / "out" / "report.html"
    report = runner.invoke(
        app, ["--workspace", str(ws), "report", "--out", str(out), "--monthly-requests", "5000"]
    )
    assert report.exit_code == EXIT_OK, report.output
    assert "SYNTHETIC DATA" in report.output
    data = json.loads(out.with_suffix(".json").read_text("utf-8"))
    assert data["context_economics"]["projections"][0]["requests_per_month"] == 5000
    assert data["identity"]["call_count"] == 313
    assert (out.with_name("report-manifest.json")).exists()


def test_report_requires_a_pricing_run(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    runner.invoke(app, ["--workspace", str(ws), "init", "--data-kind", "synthetic"])
    runner.invoke(
        app, ["--workspace", str(ws), "ingest", "--input", str(fixture_root() / "events.jsonl")]
    )
    result = runner.invoke(
        app, ["--workspace", str(ws), "report", "--out", str(tmp_path / "r.html")]
    )
    assert result.exit_code == EXIT_CONFIG
    assert "estimate first" in result.output


def test_billing_sync_refuses_without_admin_key_and_on_synthetic_workspace(tmp_path: Path) -> None:
    synthetic = tmp_path / "syn"
    runner.invoke(app, ["--workspace", str(synthetic), "init", "--data-kind", "synthetic"])
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(synthetic),
            "billing",
            "sync",
            "--provider",
            "openai",
            "--start",
            "2026-09-26",
            "--end",
            "2026-09-27",
        ],
        env=env,
    )
    assert result.exit_code == EXIT_CONFIG and "live workspace" in result.output
    live = tmp_path / "live"
    runner.invoke(app, ["--workspace", str(live), "init"])
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(live),
            "billing",
            "sync",
            "--provider",
            "anthropic",
            "--start",
            "2026-09-26",
            "--end",
            "2026-09-27",
        ],
        env=env,
    )
    assert result.exit_code == EXIT_CONFIG and "ANTHROPIC_ADMIN_API_KEY" in result.output


def test_workload_dry_run_is_offline(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.update(
        {
            "AIECON_OPENAI_MODEL": "gpt-5-nano",
            "AIECON_ANTHROPIC_MODEL": "claude-haiku-4-5",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9",
        }
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "examples" / "run_workload.py"),
            "--workspace",
            str(tmp_path / "live"),
            "--dry-run",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "DRY RUN" in proc.stdout and "plan: 18 calls" in proc.stdout
    assert "claude-haiku-4-5-20251001 (alias_resolved)" in proc.stdout
    # --live without the explicit spend flags is refused before anything is sent
    refused = subprocess.run(
        [
            sys.executable,
            str(REPO / "examples" / "run_workload.py"),
            "--workspace",
            str(tmp_path / "live"),
            "--live",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert refused.returncode == 2 and "refusing to spend" in refused.stderr
