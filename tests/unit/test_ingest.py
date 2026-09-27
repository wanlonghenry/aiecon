"""T1.2 / V01–V03 / V07: idempotent replay, conflicts, no regression, damage handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiecon import __version__
from aiecon.ingest import ingest_paths
from aiecon.spec import CallStatus, DataKind
from aiecon.storage import Storage, Workspace, WorkspaceLockedError

T0 = 1_790_467_200_000  # 2026-09-27T00:00:00Z-ish fixed base


def started(call_id: str, run_id: str, ts: int, **ctx: object) -> dict:
    return {
        "schema_version": "0.1",
        "event_id": f"ev_{call_id}_started",
        "event_type": "call_started",
        "revision": 1,
        "dataset_id": "demo-test",
        "data_kind": "synthetic",
        "source_type": "synthetic_generator",
        "source_version": "test",
        "observed_at_ms": ts,
        "occurred_at_ms": ts,
        "context": {
            "workflow_id": "support_agent",
            "workflow_run_id": run_id,
            "node_id": "reasoning",
            "node_run_id": f"{run_id}_reasoning",
            "call_id": call_id,
            "scope_id": "scope_a",
            "attempt_index": 1,
            **ctx,
        },
        "payload": {"provider": "openai", "model_requested": "fixture-openai"},
    }


def finished(
    call_id: str,
    run_id: str,
    ts: int,
    *,
    output_tokens: int = 100,
    revision: int = 1,
    event_id: str | None = None,
    **ctx: object,
) -> dict:
    return {
        "schema_version": "0.1",
        "event_id": event_id or f"ev_{call_id}_finished_{revision}",
        "event_type": "call_finished",
        "revision": revision,
        "dataset_id": "demo-test",
        "data_kind": "synthetic",
        "source_type": "synthetic_generator",
        "source_version": "test",
        "observed_at_ms": ts,
        "occurred_at_ms": ts,
        "context": {
            "workflow_id": "support_agent",
            "workflow_run_id": run_id,
            "node_id": "reasoning",
            "node_run_id": f"{run_id}_reasoning",
            "call_id": call_id,
            "scope_id": "scope_a",
            "attempt_index": 1,
            **ctx,
        },
        "payload": {
            "provider": "openai",
            "model_requested": "fixture-openai",
            "model_resolved": "fixture-openai-v1",
            "status": "success",
            "usage_format": "openai_responses",
            "usage": {
                "input_tokens": 1000,
                "input_tokens_details": {"cached_tokens": 600},
                "output_tokens": output_tokens,
            },
        },
    }


def outcome(run_id: str, ts: int, *, status: str = "succeeded", revision: int = 1) -> dict:
    return {
        "schema_version": "0.1",
        "event_id": f"ev_{run_id}_outcome_{revision}",
        "event_type": "outcome",
        "revision": revision,
        "dataset_id": "demo-test",
        "data_kind": "synthetic",
        "source_type": "synthetic_generator",
        "source_version": "test",
        "observed_at_ms": ts,
        "occurred_at_ms": ts,
        "context": {
            "workflow_id": "support_agent",
            "workflow_run_id": run_id,
            "scope_id": "scope_a",
        },
        "payload": {
            "status": status,
            "success": status == "succeeded",
            "outcome_source": "synthetic_driver",
            "terminal_at_ms": ts,
            "call_dispositions": [],
        },
    }


def write_jsonl(path: Path, events: list[dict], *, terminate: bool = True) -> Path:
    lines = [json.dumps(e, separators=(",", ":")) for e in events]
    text = "\n".join(lines) + ("\n" if terminate else "")
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    ws = Workspace(tmp_path / "ws")
    ws.init(data_kind=DataKind.synthetic, now_ms=T0, aiecon_version=__version__)
    return ws


def run_ingest(ws: Workspace, *paths: Path):
    with Storage.open(ws.db_path) as storage:
        stats = ingest_paths(storage, paths, now_ms=T0 + 1, workspace_data_kind=DataKind.synthetic)
        return stats, storage.count_calls("demo-test"), storage.count_outcomes("demo-test")


def test_replaying_fifty_calls_twice_keeps_fifty(workspace: Workspace, tmp_path: Path) -> None:
    events: list[dict] = []
    for i in range(50):
        run_id = f"run_{i // 5:03d}"
        call_id = f"call_{i:03d}"
        events.append(started(call_id, run_id, T0 + i * 1000))
        events.append(finished(call_id, run_id, T0 + i * 1000 + 500))
    for r in range(10):
        events.append(outcome(f"run_{r:03d}", T0 + 60_000 + r))
    first = write_jsonl(tmp_path / "a.jsonl", events)
    stats, calls, outcomes = run_ingest(workspace, first)
    assert stats.accepted == 110 and stats.duplicates == 0 and stats.conflicts == 0
    assert calls == 50 and outcomes == 10

    # same content under a different file name: every event is a duplicate
    second = write_jsonl(tmp_path / "b.jsonl", events)
    stats, calls, outcomes = run_ingest(workspace, second)
    assert stats.accepted == 0 and stats.duplicates == 110
    assert calls == 50 and outcomes == 10

    # same file again: skipped without re-reading
    stats, calls, _ = run_ingest(workspace, first)
    assert stats.skipped_files == 1 and calls == 50

    with Storage.open(workspace.db_path, read_only=True) as storage:
        call = storage.get_call("demo-test", "call_007")
        assert call is not None
        assert call.status is CallStatus.success
        assert call.started_at_ms == T0 + 7000
        assert call.ended_at_ms == T0 + 7500
        assert call.input_uncached_tokens == 400
        assert call.input_cache_read_tokens == 600
        assert call.input_cache_write_tokens == 0
        assert call.usage_completeness.value == "complete"
        assert len(call.source_event_ids) == 2


def test_same_event_id_different_content_is_a_conflict(
    workspace: Workspace, tmp_path: Path
) -> None:
    original = finished("call_x", "run_x", T0)
    tampered = finished("call_x", "run_x", T0, output_tokens=999)  # same event_id
    stats, calls, _ = run_ingest(workspace, write_jsonl(tmp_path / "a.jsonl", [original]))
    assert stats.accepted == 1
    stats, calls, _ = run_ingest(workspace, write_jsonl(tmp_path / "b.jsonl", [tampered]))
    assert stats.conflicts == 1 and stats.accepted == 0
    assert stats.to_dict()["conflict_event_ids"] == ["ev_call_x_finished_1"]
    with Storage.open(workspace.db_path, read_only=True) as storage:
        call = storage.get_call("demo-test", "call_x")
        assert call is not None and call.output_tokens == 100  # original preserved


def test_finish_before_start_never_regresses(workspace: Workspace, tmp_path: Path) -> None:
    events = [finished("call_y", "run_y", T0 + 500), started("call_y", "run_y", T0)]
    stats, calls, _ = run_ingest(workspace, write_jsonl(tmp_path / "a.jsonl", events))
    assert stats.accepted == 2 and calls == 1
    with Storage.open(workspace.db_path, read_only=True) as storage:
        call = storage.get_call("demo-test", "call_y")
        assert call is not None
        assert call.status is CallStatus.success
        assert call.started_at_ms == T0 and call.ended_at_ms == T0 + 500
        assert call.output_tokens == 100


def test_repeated_terminal_with_new_event_id_is_not_double_counted(
    workspace: Workspace, tmp_path: Path
) -> None:
    first = finished("call_z", "run_z", T0)
    again = finished("call_z", "run_z", T0, event_id="ev_call_z_redelivered")
    stats, calls, _ = run_ingest(workspace, write_jsonl(tmp_path / "a.jsonl", [first, again]))
    assert calls == 1
    assert stats.accepted == 1 and stats.repeated_terminal == 1 and stats.conflicts == 0


def test_higher_revision_replaces_and_lower_is_superseded(
    workspace: Workspace, tmp_path: Path
) -> None:
    rev1 = finished("call_r", "run_r", T0, output_tokens=100, revision=1)
    rev2 = finished("call_r", "run_r", T0, output_tokens=120, revision=2)
    stale = finished(
        "call_r", "run_r", T0, output_tokens=100, revision=1, event_id="ev_call_r_stale"
    )
    stats, calls, _ = run_ingest(workspace, write_jsonl(tmp_path / "a.jsonl", [rev1, rev2, stale]))
    assert calls == 1
    assert stats.accepted == 2 and stats.superseded == 1
    with Storage.open(workspace.db_path, read_only=True) as storage:
        call = storage.get_call("demo-test", "call_r")
        assert call is not None and call.output_tokens == 120 and call.revision == 2
        assert set(call.source_event_ids) == {"ev_call_r_finished_1", "ev_call_r_finished_2"}


def test_same_revision_different_body_is_a_conflict(workspace: Workspace, tmp_path: Path) -> None:
    a = finished("call_c", "run_c", T0, output_tokens=100)
    b = finished("call_c", "run_c", T0, output_tokens=101, event_id="ev_call_c_other")
    stats, calls, _ = run_ingest(workspace, write_jsonl(tmp_path / "a.jsonl", [a, b]))
    assert calls == 1 and stats.conflicts == 1
    with Storage.open(workspace.db_path, read_only=True) as storage:
        call = storage.get_call("demo-test", "call_c")
        assert call is not None and call.output_tokens == 100


def test_outcome_revisions(workspace: Workspace, tmp_path: Path) -> None:
    events = [
        outcome("run_o", T0, status="pending"),
        outcome("run_o", T0 + 10, status="failed", revision=2),
    ]
    # pending outcomes carry no terminal timestamp
    events[0]["payload"]["terminal_at_ms"] = None
    stats, _, outcomes = run_ingest(workspace, write_jsonl(tmp_path / "a.jsonl", events))
    assert outcomes == 1 and stats.accepted == 2
    with Storage.open(workspace.db_path, read_only=True) as storage:
        got = storage.get_outcome("demo-test", "run_o")
        assert got is not None and got.status.value == "failed" and got.revision == 2


def test_damaged_lines_are_counted_without_echoing_content(
    workspace: Workspace, tmp_path: Path
) -> None:
    canary = "CANARY-SECRET-PROMPT-31337"
    good1 = json.dumps(finished("call_d1", "run_d", T0))
    broken = '{"event_id": "ev_broken", "prompt": "' + canary + '", "usage": {'  # malformed JSON
    leaked = finished("call_d2", "run_d", T0 + 1)
    leaked["payload"]["messages"] = [{"role": "user", "content": canary}]  # forbidden field
    good2 = json.dumps(finished("call_d3", "run_d", T0 + 2))
    tail = json.dumps(finished("call_d4", "run_d", T0 + 3))  # unterminated
    path = tmp_path / "damaged.jsonl"
    path.write_text("\n".join([good1, broken, json.dumps(leaked), good2, tail]), encoding="utf-8")

    stats, calls, _ = run_ingest(workspace, path)
    assert calls == 2
    assert stats.accepted == 2 and stats.rejected == 2 and stats.truncated_tail_files == 1
    summary = json.dumps(stats.to_dict())
    assert canary not in summary
    reasons = {line: err for line, err in stats.files[0].rejected_lines}
    assert reasons[2] == "json_decode_error"
    assert reasons[3].startswith("validation_error(")
    # nothing with the canary reached the database
    with Storage.open(workspace.db_path, read_only=True) as storage:
        for table in ("calls", "outcomes", "meta_events", "meta_ingest_files"):
            rows = storage.query(f"SELECT * FROM {table}")
            assert canary not in json.dumps(rows, default=str)


def test_data_kind_mismatch_is_rejected(workspace: Workspace, tmp_path: Path) -> None:
    live = finished("call_l", "run_l", T0)
    live["data_kind"] = "live"
    stats, calls, _ = run_ingest(workspace, write_jsonl(tmp_path / "a.jsonl", [live]))
    assert calls == 0 and stats.rejected == 1
    assert stats.files[0].rejected_lines == [(1, "data_kind_mismatch")]


def test_workspace_lock_is_exclusive(workspace: Workspace) -> None:
    with workspace.lock():
        with pytest.raises(WorkspaceLockedError):
            with workspace.lock():
                pass
    # released afterwards
    with workspace.lock():
        pass


def test_workspace_refuses_other_data_kind(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    ws.init(data_kind=DataKind.synthetic, now_ms=T0, aiecon_version=__version__)
    with pytest.raises(Exception, match="refusing"):
        ws.init(data_kind=DataKind.live, now_ms=T0, aiecon_version=__version__)
