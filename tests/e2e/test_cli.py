"""Offline CLI acceptance: demo, init, ingest exit codes, doctor, schema export."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from aiecon.cli import EXIT_CONFIG, EXIT_CONTRACT, EXIT_OK, app

runner = CliRunner()


def test_demo_runs_offline_and_is_rebuildable(tmp_path: Path) -> None:
    out = tmp_path / "demo"
    result = runner.invoke(app, ["demo", "--out", str(out)])
    assert result.exit_code == EXIT_OK, result.output
    assert "SYNTHETIC DEMO" in result.output
    assert "313 (expected 313)" in result.output
    assert (out / "aiecon.duckdb").exists()
    assert (out / "raw" / "2026-09-26" / "events.jsonl").exists()
    assert (out / "provider" / "fixtures" / "anthropic_cost.manifest.json").exists()

    again = runner.invoke(app, ["demo", "--out", str(out)])
    assert again.exit_code == EXIT_OK, again.output
    assert "313 (expected 313)" in again.output


def test_demo_refuses_a_live_workspace(tmp_path: Path) -> None:
    live = tmp_path / "live"
    created = runner.invoke(app, ["--workspace", str(live), "init"])
    assert created.exit_code == EXIT_OK, created.output
    assert "data_kind" in created.output and "live" in created.output
    result = runner.invoke(app, ["demo", "--out", str(live)])
    assert result.exit_code == EXIT_CONFIG
    assert "refusing" in result.output


def test_init_is_idempotent_and_doctor_offline_passes(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    first = runner.invoke(app, ["--workspace", str(ws), "init", "--data-kind", "synthetic"])
    second = runner.invoke(app, ["--workspace", str(ws), "init", "--data-kind", "synthetic"])
    assert first.exit_code == EXIT_OK and second.exit_code == EXIT_OK
    doctor = runner.invoke(app, ["doctor", "--mode", "offline"])
    assert doctor.exit_code == EXIT_OK, doctor.output
    assert "checks passed" in doctor.output


def test_ingest_conflict_exits_with_contract_code(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert (
        runner.invoke(app, ["--workspace", str(ws), "init", "--data-kind", "synthetic"]).exit_code
        == 0
    )
    base = {
        "schema_version": "0.1",
        "event_id": "ev_1",
        "event_type": "call_finished",
        "revision": 1,
        "dataset_id": "d",
        "data_kind": "synthetic",
        "source_type": "synthetic_generator",
        "source_version": "t",
        "observed_at_ms": 10,
        "occurred_at_ms": 10,
        "context": {"call_id": "c1", "scope_id": "s"},
        "payload": {
            "provider": "openai",
            "model_requested": "fixture-openai",
            "status": "success",
            "usage_format": "openai_responses",
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 1,
            },
        },
    }
    a = tmp_path / "a.jsonl"
    a.write_text(json.dumps(base) + "\n", encoding="utf-8")
    ok = runner.invoke(app, ["--workspace", str(ws), "ingest", "--input", str(a)])
    assert ok.exit_code == EXIT_OK, ok.output
    tampered = dict(base)
    tampered["payload"] = dict(
        base["payload"], usage={**base["payload"]["usage"], "output_tokens": 2}
    )
    b = tmp_path / "b.jsonl"
    b.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
    conflict = runner.invoke(app, ["--workspace", str(ws), "ingest", "--input", str(b)])
    assert conflict.exit_code == EXIT_CONTRACT, conflict.output
    assert "conflict" in conflict.output


def test_ingest_without_init_is_a_configuration_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["--workspace", str(tmp_path / "nope"), "ingest", "--input", str(tmp_path)]
    )
    assert result.exit_code == EXIT_CONFIG
    assert "not initialised" in result.output


def test_schema_export_needs_no_database(tmp_path: Path) -> None:
    out = tmp_path / "schemas"
    result = runner.invoke(app, ["schema", "export", "--out", str(out)])
    assert result.exit_code == EXIT_OK, result.output
    names = {p.name for p in out.iterdir()}
    assert {"RawEnvelope.schema.json", "ModelCall.schema.json", "CostLineItem.schema.json"} <= names
