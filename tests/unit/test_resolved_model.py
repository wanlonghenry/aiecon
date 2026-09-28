"""A provider-reported model id is kept only when it can be one (seen live: route names)."""

from __future__ import annotations

from aiecon.adapters.base import model_family, resolved_model_or_none
from aiecon.adapters.normalize import normalize
from aiecon.spec import Provider, RawEnvelope

FINISHED = {
    "schema_version": "0.1",
    "event_id": "ev_call_test_finished_1",
    "event_type": "call_finished",
    "revision": 1,
    "dataset_id": "live-test",
    "data_kind": "live",
    "source_type": "litellm_callback",
    "source_version": "litellm==1.102.1",
    "observed_at_ms": 1790567513150,
    "occurred_at_ms": 1790567513150,
    "context": {
        "workflow_id": "workload",
        "workflow_run_id": "wl_x_stream",
        "node_id": "answer",
        "node_run_id": "wl_x_stream_answer",
        "call_id": "call_test",
        "scope_id": "live_anthropic_workspace",
        "attempt_index": 1,
    },
    "payload": {
        "provider": "anthropic",
        "api_family": "messages",
        "model_requested": "claude-haiku-4-5-20251001",
        "model_resolved": "anthropic-live",
        "stream": True,
        "status": "success",
        "started_at_ms": 1790567512530,
        "usage_format": "litellm_standard",
        "usage": {"prompt_tokens": 18, "completion_tokens": 13, "total_tokens": 31},
    },
}


def test_model_family_uses_prefix_then_known_bare_families() -> None:
    assert model_family("anthropic/claude-haiku-4-5") is Provider.anthropic
    assert model_family("azure/gpt-5") is Provider.other
    assert model_family("gpt-5-nano-2025-08-07") is Provider.openai
    assert model_family("o3-mini") is Provider.openai
    assert model_family("claude-sonnet-5") is Provider.anthropic
    assert model_family("anthropic-live") is None
    assert model_family("fixture-anthropic-v1") is None
    assert model_family(None) is None


def test_resolved_model_rule() -> None:
    a, o = Provider.anthropic, Provider.openai
    # route name reported as the model of a streamed Anthropic response: unknown
    assert resolved_model_or_none(a, "claude-haiku-4-5-20251001", "anthropic-live") is None
    # dated ids and bare aliases of the same family are kept
    assert (
        resolved_model_or_none(o, "gpt-5-nano", "gpt-5-nano-2025-08-07") == "gpt-5-nano-2025-08-07"
    )
    assert resolved_model_or_none(o, "gpt-5-nano", "gpt-5-nano") == "gpt-5-nano"
    # another provider's family can never be this provider's model
    assert resolved_model_or_none(a, "claude-haiku-4-5", "gpt-5") is None
    # synthetic fixtures: neither name has a known family, so the resolved id stays
    assert (
        resolved_model_or_none(a, "fixture-anthropic", "fixture-anthropic-v1")
        == "fixture-anthropic-v1"
    )
    assert resolved_model_or_none(a, "claude-haiku-4-5", None) is None


def test_normalizer_discards_route_names_on_replay() -> None:
    call = normalize(RawEnvelope.model_validate(FINISHED))
    assert call.model_resolved is None
    assert call.model_requested == "claude-haiku-4-5-20251001"
    assert call.input_total_tokens == 18 and call.output_tokens == 13
