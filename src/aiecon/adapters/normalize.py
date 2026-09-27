"""``normalize(envelope) -> ModelCall`` (PLAN.md §6.2): pure, format-driven, clock-free."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiecon.adapters import anthropic, litellm, openai
from aiecon.adapters.base import NormalizedUsage, finish
from aiecon.spec.call import ModelCall, UsageCompleteness
from aiecon.spec.common import NORMALIZER_VERSION
from aiecon.spec.envelope import (
    CallFinishedPayload,
    CallStartedPayload,
    CallStatus,
    EventType,
    RawEnvelope,
    UsageFormat,
)

_ADAPTERS = {
    UsageFormat.openai_chat_completions: openai.normalize_chat_completions,
    UsageFormat.openai_responses: openai.normalize_responses,
    UsageFormat.anthropic_messages: anthropic.normalize_messages,
    UsageFormat.litellm_standard: litellm.normalize_litellm,
}

USAGE_ALLOWLISTS: dict[UsageFormat, Mapping[str, Any]] = {
    UsageFormat.openai_chat_completions: openai.CHAT_COMPLETIONS_ALLOWLIST,
    UsageFormat.openai_responses: openai.RESPONSES_ALLOWLIST,
    UsageFormat.anthropic_messages: anthropic.MESSAGES_ALLOWLIST,
    UsageFormat.litellm_standard: litellm.LITELLM_ALLOWLIST,
}


def normalize_usage(usage_format: UsageFormat, usage: Mapping[str, Any] | None) -> NormalizedUsage:
    adapter = _ADAPTERS.get(usage_format)
    if adapter is None:
        result = NormalizedUsage()
        result.note("usage_format_unknown")
        return finish(result)
    return adapter(usage)


def normalize(envelope: RawEnvelope) -> ModelCall:
    """Project one call event onto the ModelCall shape.

    ``call_started`` yields an ``in_flight`` projection; ``call_finished`` yields the terminal
    projection. Merging start and finish is the ingest layer's job.
    """

    if envelope.event_type is EventType.outcome:
        raise ValueError("outcome events do not normalize to a ModelCall")
    ctx = envelope.context
    assert ctx.call_id is not None
    base: dict[str, Any] = {
        "dataset_id": envelope.dataset_id,
        "call_id": ctx.call_id,
        "data_kind": envelope.data_kind,
        "scope_id": ctx.scope_id,
        "workflow_id": ctx.workflow_id,
        "workflow_run_id": ctx.workflow_run_id,
        "node_id": ctx.node_id,
        "node_run_id": ctx.node_run_id,
        "attempt_index": ctx.attempt_index,
        "retry_of_call_id": ctx.retry_of_call_id,
        "fallback_of_call_id": ctx.fallback_of_call_id,
        "trace_id": ctx.trace_id,
        "span_id": ctx.span_id,
        "source_event_ids": [envelope.event_id],
        "source_content_hashes": [envelope.body_hash()],
        "normalizer_version": NORMALIZER_VERSION,
        "revision": envelope.revision,
    }
    payload = envelope.payload
    if isinstance(payload, CallStartedPayload):
        return ModelCall(
            **base,
            provider=payload.provider,
            api_family=payload.api_family,
            model_requested=payload.model_requested,
            model_resolved=payload.model_resolved,
            service_tier=payload.service_tier,
            inference_region=payload.inference_region,
            stream=payload.stream,
            started_at_ms=envelope.occurred_at_ms,
            status=CallStatus.in_flight,
            usage_completeness=UsageCompleteness.missing,
            prefix_fingerprint=payload.prefix_fingerprint,
            fingerprint_key_id=payload.fingerprint_key_id,
            prefix_tokens=payload.prefix_tokens,
            prefix_token_count_method=payload.prefix_token_count_method,
            cache_policy=payload.cache_policy,
        )
    assert isinstance(payload, CallFinishedPayload)
    usage = normalize_usage(payload.usage_format, payload.usage)
    return ModelCall(
        **base,
        provider=payload.provider,
        api_family=payload.api_family,
        model_requested=payload.model_requested,
        model_resolved=payload.model_resolved,
        service_tier=payload.service_tier,
        inference_region=payload.inference_region,
        provider_request_id=payload.provider_request_id,
        stream=payload.stream,
        started_at_ms=payload.started_at_ms,
        ended_at_ms=envelope.occurred_at_ms,
        status=payload.status,
        error_class=payload.error_class,
        usage_format=payload.usage_format,
        input_total_tokens=usage.input_total,
        input_uncached_tokens=usage.input_uncached,
        input_cache_read_tokens=usage.input_cache_read,
        input_cache_write_tokens=usage.input_cache_write,
        output_tokens=usage.output,
        cache_write_breakdown=usage.cache_write_breakdown,
        usage_completeness=usage.completeness,
        usage_notes=list(usage.notes),
        provider_usage_safe=payload.usage,
        upstream_cost_estimate_usd=payload.upstream_cost_estimate_usd,
        schema_drift=payload.schema_drift,
        prefix_fingerprint=payload.prefix_fingerprint,
        fingerprint_key_id=payload.fingerprint_key_id,
        prefix_tokens=payload.prefix_tokens,
        prefix_token_count_method=payload.prefix_token_count_method,
        cache_policy=payload.cache_policy,
    )
