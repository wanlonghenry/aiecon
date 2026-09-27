"""G01: terminal states through the locked LiteLLM call path (Router + mock responses).

Skipped when the ``live`` extra is not installed. No provider request is made: every
response is a LiteLLM mock. Covers non-streaming, consumed streaming, an abandoned stream
and a failed attempt, plus the "first terminal wins" rule.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

litellm = pytest.importorskip("litellm")

from litellm import Router  # noqa: E402

from aiecon import __version__  # noqa: E402
from aiecon.collect.litellm_callback import (  # noqa: E402
    AieconLiteLLMLogger,
    CollectorConfig,
    EnvelopeCollector,
)
from aiecon.collect.writer import JsonlWriter  # noqa: E402
from aiecon.ingest import ingest_paths  # noqa: E402
from aiecon.spec import CallStatus, DataKind, UsageCompleteness  # noqa: E402
from aiecon.storage import Storage, Workspace  # noqa: E402


def meta(run: str) -> dict:
    return {
        "aiecon": {
            "workflow_id": "router_probe",
            "workflow_run_id": run,
            "node_id": "answer",
            "node_run_id": f"{run}_answer",
        }
    }


async def drive(collector: EnvelopeCollector) -> None:
    logger = AieconLiteLLMLogger(collector)
    previous = list(litellm.callbacks)
    litellm.callbacks = [logger]
    try:
        router = Router(
            model_list=[
                {
                    "model_name": "openai-live",
                    "litellm_params": {"model": "gpt-5-nano", "api_key": "sk-mock"},
                }
            ],
            num_retries=0,
        )
        await router.acompletion(
            model="openai-live",
            messages=[{"role": "user", "content": "SECRET-A"}],
            mock_response="plain answer",
            metadata=meta("run_plain"),
        )
        stream = await router.acompletion(
            model="openai-live",
            messages=[{"role": "user", "content": "SECRET-B"}],
            mock_response="streamed answer with several words",
            stream=True,
            stream_options={"include_usage": True},
            metadata=meta("run_stream"),
        )
        async for _chunk in stream:
            pass
        abandoned = await router.acompletion(
            model="openai-live",
            messages=[{"role": "user", "content": "SECRET-C"}],
            mock_response="abandoned stream",
            stream=True,
            metadata=meta("run_abandoned"),
        )
        async for _chunk in abandoned:
            break
        with pytest.raises(Exception):  # noqa: B017 - the mock raises the provider error
            await router.acompletion(
                model="openai-live",
                messages=[{"role": "user", "content": "SECRET-D"}],
                mock_response=litellm.RateLimitError(
                    "mocked rate limit", llm_provider="openai", model="gpt-5-nano"
                ),
                metadata=meta("run_failed"),
            )
        await asyncio.sleep(2.0)  # let LiteLLM's async logging tasks finish
    finally:
        litellm.callbacks = previous


def test_router_paths_produce_one_terminal_per_attempt(tmp_path: Path) -> None:
    writer = JsonlWriter(tmp_path / "raw")
    collector = EnvelopeCollector(
        writer=writer, config=CollectorConfig(dataset_id="router-probe", scope_id="probe")
    )
    asyncio.run(drive(collector))
    health = collector.health()
    assert health["failed"] == 0 and health["hook_errors"] == 0
    # the non-streaming attempt got both a deployment hook and a log event: one was ignored
    assert health["terminal_duplicates_ignored"] >= 1

    ws = Workspace(tmp_path / "ws")
    ws.init(data_kind=DataKind.live, now_ms=1, aiecon_version=__version__)
    with Storage.open(ws.db_path) as storage:
        stats = ingest_paths(
            storage, [tmp_path / "raw"], now_ms=2, workspace_data_kind=DataKind.live
        )
        assert stats.rejected == 0 and stats.conflicts == 0
        calls = {c.workflow_run_id: c for c in storage.list_calls("router-probe")}
    assert set(calls) == {"run_plain", "run_stream", "run_abandoned", "run_failed"}

    plain = calls["run_plain"]
    assert plain.status is CallStatus.success
    assert plain.usage_completeness is UsageCompleteness.complete
    assert plain.input_total_tokens == 10 and plain.output_tokens == 20  # LiteLLM mock usage
    assert plain.ended_at_ms is not None and plain.started_at_ms is not None
    assert plain.ended_at_ms >= plain.started_at_ms  # local clock, never provider `created`

    streamed = calls["run_stream"]
    assert streamed.status is CallStatus.success, "streaming terminal must come from the log event"
    assert streamed.stream is True
    assert streamed.usage_completeness is UsageCompleteness.complete
    assert streamed.output_tokens is not None and streamed.output_tokens > 0
    assert streamed.ended_at_ms is not None and streamed.ended_at_ms >= (
        streamed.started_at_ms or 0
    )

    abandoned = calls["run_abandoned"]
    assert abandoned.status is CallStatus.in_flight  # documented: no terminal event exists
    assert abandoned.usage_completeness is UsageCompleteness.missing

    failed = calls["run_failed"]
    assert failed.status is CallStatus.error and failed.error_class == "rate_limit"
    assert failed.provider.value == "openai"

    raw = "".join(p.read_text("utf-8") for p in (tmp_path / "raw").rglob("*.jsonl"))
    for secret in ("SECRET-A", "SECRET-B", "SECRET-C", "SECRET-D", "sk-mock", "api_key"):
        assert secret not in raw
