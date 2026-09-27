"""Collector against the kwargs shape observed from LiteLLM 1.102.1 deployment hooks."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from aiecon.collect.litellm_callback import (
    CollectorConfig,
    EnvelopeCollector,
    provider_from_model_name,
)
from aiecon.collect.writer import JsonlWriter
from aiecon.spec import RawEnvelope


class AuthError(Exception):
    status_code = 401
    llm_provider = "anthropic"


def real_shape(model: str, group: str) -> dict:
    # observed 2026-09-27: bare model id, metadata dict, api_key present, no litellm_params
    return {
        "model": model,
        "messages": [{"role": "user", "content": "SECRET-BODY"}],
        "max_tokens": 5,
        "stream": False,
        "api_key": "sk-this-must-never-be-persisted",
        "litellm_call_id": "9f1c1c3e-1111-4a2b-9c3d-000000000001",
        "litellm_trace_id": "9f1c1c3e-2222-4a2b-9c3d-000000000002",
        "metadata": {
            "model_group": group,
            "deployment_model_name": model,
            "attempted_retries": 0,
            "aiecon": {
                "workflow_id": "smoke",
                "workflow_run_id": "smoke_run_1",
                "node_id": "answer",
                "node_run_id": "smoke_run_1_answer",
            },
        },
    }


def test_provider_from_bare_model_ids() -> None:
    assert provider_from_model_name("gpt-5-nano").value == "openai"
    assert provider_from_model_name("claude-haiku-4-5").value == "anthropic"
    assert provider_from_model_name("openai/gpt-5-nano").value == "openai"
    assert provider_from_model_name("anthropic/claude-sonnet-5").value == "anthropic"
    assert provider_from_model_name("groq/llama-3").value == "other"
    assert provider_from_model_name(None) is None


def test_real_hook_shape_yields_provider_lineage_and_no_secrets(tmp_path: Path) -> None:
    writer = JsonlWriter(tmp_path / "raw")
    collector = EnvelopeCollector(writer=writer, config=CollectorConfig(dataset_id="live-x"))
    first = collector.begin_attempt(real_shape("gpt-5-nano", "openai-live"), "completion")
    assert first["metadata"]["aiecon_call_id"].startswith("call_")
    assert "litellm_params" not in first  # nothing invented on the request
    # LiteLLM hands the failure hook a read-only view of the attempt kwargs
    collector.finish_failure(MappingProxyType(first), AuthError("bad key"), "completion")
    second = collector.begin_attempt(real_shape("claude-haiku-4-5", "anthropic-live"), "completion")
    collector.finish_failure(MappingProxyType(second), AuthError("bad key"), "completion")

    lines = [
        RawEnvelope.model_validate_json(line)
        for p in (tmp_path / "raw").rglob("*.jsonl")
        for line in p.read_text("utf-8").splitlines()
    ]
    assert len(lines) == 4
    started = [e for e in lines if e.event_type.value == "call_started"]
    finished = [e for e in lines if e.event_type.value == "call_finished"]
    assert [e.payload.provider.value for e in started] == ["openai", "anthropic"]
    assert [e.payload.provider.value for e in finished] == ["openai", "anthropic"]
    assert finished[0].payload.error_class == "auth" and finished[0].payload.status.value == "error"
    # second attempt for the same node run on a different model group is a fallback
    assert started[1].context.fallback_of_call_id == started[0].context.call_id
    assert started[1].context.attempt_index == 2
    # LiteLLM ids are kept as opaque correlation ids
    assert started[0].context.trace_id == "9f1c1c3e-2222-4a2b-9c3d-000000000002"
    assert started[0].context.span_id == "9f1c1c3e-1111-4a2b-9c3d-000000000001"
    raw = "".join(p.read_text("utf-8") for p in (tmp_path / "raw").rglob("*.jsonl"))
    assert "sk-this-must-never-be-persisted" not in raw
    assert "SECRET-BODY" not in raw
    assert "api_key" not in raw
