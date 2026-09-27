"""T2.2 / V06 / V07: per-attempt collection, lineage, redelivery and canary containment."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from aiecon import __version__
from aiecon.collect.litellm_callback import CollectorConfig, EnvelopeCollector
from aiecon.collect.writer import JsonlWriter
from aiecon.ingest import ingest_paths
from aiecon.spec import CallStatus, DataKind
from aiecon.storage import Storage, Workspace

CANARY = "CANARY-PROMPT-7c1e2b9d"
T0 = 1_790_467_200_000


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> int:
        self.now += 250
        return self.now


class RateLimitError(Exception):
    status_code = 429


def request(model: str, node_run: str = "run_x_reasoning", model_group: str | None = None) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": CANARY},
            {"role": "user", "content": "hi " + CANARY},
        ],
        "max_tokens": 200,
        "stream": False,
        "litellm_params": {
            "custom_llm_provider": "anthropic" if "claude" in model else "openai",
            "model_group": model_group or model,
            "metadata": {
                "user_api_key_alias": CANARY,
                "aiecon": {
                    "workflow_id": "support_agent",
                    "workflow_run_id": "run_x",
                    "node_id": "reasoning",
                    "node_run_id": node_run,
                    "scope_id": "live_scope_test",
                },
            },
        },
    }


def response(model: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="chatcmpl-abc123",
        created=T0 // 1000 + 10,
        model=model,
        choices=[{"message": {"content": CANARY}}],
        usage={
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "total_tokens": 1050,
            "prompt_tokens_details": {"cached_tokens": 600},
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 600,
            "surprising_field": 7,
            "nested_text": {"content": CANARY},
        },
        _hidden_params={"response_cost": 0.0021, "api_base": "https://example.invalid/" + CANARY},
    )


def make_collector(tmp_path: Path) -> tuple[EnvelopeCollector, Path]:
    raw = tmp_path / "raw"
    writer = JsonlWriter(raw, clock=Clock())
    collector = EnvelopeCollector(
        writer=writer,
        config=CollectorConfig(dataset_id="live-test", data_kind=DataKind.live, scope_id="s"),
        clock=Clock(),
    )
    return collector, raw


def ingest(tmp_path: Path, raw: Path):
    ws = Workspace(tmp_path / "ws")
    ws.init(data_kind=DataKind.live, now_ms=1, aiecon_version=__version__)
    storage = Storage.open(ws.db_path)
    stats = ingest_paths(storage, [raw], now_ms=2, workspace_data_kind=DataKind.live)
    return storage, stats


def test_three_attempt_chain_produces_three_calls_with_explicit_lineage(tmp_path: Path) -> None:
    collector, raw = make_collector(tmp_path)
    first = collector.begin_attempt(request("gpt-fixture"), "completion")
    collector.finish_failure(first, RateLimitError("boom " + CANARY), "completion")
    second = collector.begin_attempt(request("gpt-fixture"), "completion")
    collector.finish_failure(
        second, RateLimitError("boom again"), "completion", fallback_depth=None
    )
    third = collector.begin_attempt(request("claude-fixture"), "completion")
    collector.finish_success(third, response("claude-fixture-v1"), "completion")
    assert collector.writer.health()["written"] == 6

    storage, stats = ingest(tmp_path, raw)
    try:
        assert stats.accepted == 6 and stats.rejected == 0
        calls = sorted(storage.list_calls("live-test"), key=lambda c: c.attempt_index or 0)
        assert len(calls) == 3
        assert [c.attempt_index for c in calls] == [1, 2, 3]
        assert [c.status for c in calls] == [CallStatus.error, CallStatus.error, CallStatus.success]
        assert (
            calls[0].error_class == "rate_limit" and calls[0].usage_completeness.value == "missing"
        )
        assert calls[1].retry_of_call_id == calls[0].call_id  # same model group -> retry
        assert calls[2].fallback_of_call_id == calls[1].call_id  # different group -> fallback
        assert calls[2].provider.value == "anthropic"
        assert calls[2].input_total_tokens == 1000
        assert calls[2].input_cache_read_tokens == 600 and calls[2].input_uncached_tokens == 400
        assert calls[2].upstream_cost_estimate_usd is not None
        assert calls[2].schema_drift == {"surprising_field": 1, "nested_text": 1}
        assert calls[2].started_at_ms is not None and calls[2].ended_at_ms > calls[2].started_at_ms
    finally:
        storage.close()


def test_redelivered_success_hook_does_not_create_a_fourth_call(tmp_path: Path) -> None:
    collector, raw = make_collector(tmp_path)
    kwargs = collector.begin_attempt(request("gpt-fixture"), "completion")
    collector.finish_success(kwargs, response("gpt-fixture-v1"), "completion")
    collector.finish_success(kwargs, response("gpt-fixture-v1"), "completion")  # redelivery
    # the first terminal state stands; the redelivery is counted and writes nothing
    assert collector.writer.health()["written"] == 2
    assert collector.health()["terminal_duplicates_ignored"] == 1
    storage, stats = ingest(tmp_path, raw)
    try:
        assert storage.count_calls("live-test") == 1
        assert stats.accepted == 2 and stats.duplicates == 0 and stats.conflicts == 0
    finally:
        storage.close()


def test_canary_never_reaches_disk_or_database(tmp_path: Path, capsys) -> None:
    collector, raw = make_collector(tmp_path)
    kwargs = collector.begin_attempt(request("gpt-fixture"), "completion")
    collector.finish_failure(kwargs, RateLimitError(CANARY), "completion")
    kwargs2 = collector.begin_attempt(request("gpt-fixture"), "completion")
    collector.finish_success(kwargs2, response("gpt-fixture-v1"), "completion")
    for path in raw.rglob("*.jsonl"):
        assert CANARY not in path.read_text("utf-8")
    storage, _ = ingest(tmp_path, raw)
    try:
        for table in ("calls", "meta_events"):
            assert CANARY not in json.dumps(storage.query(f"SELECT * FROM {table}"), default=str)
    finally:
        storage.close()
    captured = capsys.readouterr()
    assert CANARY not in captured.out and CANARY not in captured.err


def test_hooks_survive_garbage_input_and_count_errors(tmp_path: Path) -> None:
    collector, raw = make_collector(tmp_path)
    result = collector._guard(lambda: collector.begin_attempt(None, "completion"), {"x": 1})  # type: ignore[arg-type]
    assert result == {"x": 1}
    assert collector.health()["hook_errors"] == 1
    # a finish without a start still records the attempt instead of dropping it
    ok = collector.finish_success(request("gpt-fixture"), response("gpt-fixture-v1"), "completion")
    assert ok is True
    storage, stats = ingest(tmp_path, raw)
    try:
        assert storage.count_calls("live-test") == 1
        (call,) = storage.list_calls("live-test")
        assert call.started_at_ms is None and call.status is CallStatus.success
    finally:
        storage.close()


def test_writer_counts_failures_instead_of_raising(tmp_path: Path) -> None:
    blocker = tmp_path / "raw"
    blocker.write_text("not a directory", encoding="utf-8")
    writer = JsonlWriter(blocker, clock=Clock())
    collector = EnvelopeCollector(
        writer=writer, config=CollectorConfig(dataset_id="live-test"), clock=Clock()
    )
    collector.begin_attempt(request("gpt-fixture"), "completion")
    health = collector.health()
    assert health["failed"] == 1 and health["written"] == 0
    assert health["last_error_class"] in {
        "NotADirectoryError",
        "FileExistsError",
        "FileNotFoundError",
        "OSError",
    }


def test_started_kwargs_do_not_mutate_shared_metadata(tmp_path: Path) -> None:
    collector, _ = make_collector(tmp_path)
    original = request("gpt-fixture")
    shared_metadata = original["litellm_params"]["metadata"]
    first = collector.begin_attempt(original, "completion")
    second = collector.begin_attempt(original, "completion")
    assert "aiecon_call_id" not in shared_metadata
    assert (
        first["litellm_params"]["metadata"]["aiecon_call_id"]
        != second["litellm_params"]["metadata"]["aiecon_call_id"]
    )
