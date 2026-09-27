"""RawEnvelope: the append-only, allowlisted telemetry record (PLAN.md §4.2).

Three event types exist in v0.1: ``call_started``, ``call_finished`` and ``outcome``.
Provider snapshots never travel as envelopes; they have their own importer.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from aiecon.spec.common import (
    SCHEMA_VERSION,
    ApiFamily,
    CodeStr,
    DataKind,
    DecimalStr,
    HexStr,
    IdStr,
    IntMap,
    NonNegInt,
    PosInt,
    Provider,
    SafeUsage,
    SpecModel,
    UtcMs,
    VersionStr,
)


class EventType(StrEnum):
    call_started = "call_started"
    call_finished = "call_finished"
    outcome = "outcome"


class SourceType(StrEnum):
    litellm_callback = "litellm_callback"
    synthetic_generator = "synthetic_generator"
    application_sdk = "application_sdk"


class UsageFormat(StrEnum):
    """Which parser to use. Chosen by format, never by provider name alone (§5.1)."""

    openai_chat_completions = "openai_chat_completions"
    openai_responses = "openai_responses"
    anthropic_messages = "anthropic_messages"
    litellm_standard = "litellm_standard"
    unknown = "unknown"


class CallStatus(StrEnum):
    success = "success"
    error = "error"
    timeout = "timeout"
    cancelled = "cancelled"
    in_flight = "in_flight"


class OutcomeStatus(StrEnum):
    succeeded = "succeeded"
    failed = "failed"
    abandoned = "abandoned"
    pending = "pending"
    unknown = "unknown"


class OutcomeSource(StrEnum):
    synthetic_driver = "synthetic_driver"
    application_label = "application_label"


class Disposition(StrEnum):
    used = "used"
    discarded = "discarded"
    unknown = "unknown"


# Documented cache_policy codes. Kept as CodeStr so adapters can record provider-specific
# policies without a schema change; anything outside this list is "unverified".
KNOWN_CACHE_POLICIES: tuple[str, ...] = (
    "none",
    "provider_auto",
    "ephemeral_5m",
    "ephemeral_1h",
    "unknown",
)

# Documented error classes. Adapters map provider exceptions onto these codes and never
# persist the exception text.
KNOWN_ERROR_CLASSES: tuple[str, ...] = (
    "rate_limit",
    "auth",
    "invalid_request",
    "not_found",
    "server_error",
    "overloaded",
    "timeout",
    "connection",
    "cancelled",
    "content_filter",
    "context_length",
    "unknown",
)


class EventContext(SpecModel):
    """Business correlation. Missing workflow/node labels mean ``unattributed`` (§4.1)."""

    workflow_id: IdStr | None = None
    workflow_run_id: IdStr | None = None
    node_id: IdStr | None = None
    node_run_id: IdStr | None = None
    call_id: IdStr | None = None
    scope_id: IdStr
    attempt_index: PosInt | None = None
    retry_of_call_id: IdStr | None = None
    fallback_of_call_id: IdStr | None = None
    trace_id: IdStr | None = None
    span_id: IdStr | None = None

    @model_validator(mode="after")
    def _lineage_is_explicit(self) -> EventContext:
        if self.retry_of_call_id and self.retry_of_call_id == self.call_id:
            raise ValueError("retry_of_call_id must reference a different call")
        if self.fallback_of_call_id and self.fallback_of_call_id == self.call_id:
            raise ValueError("fallback_of_call_id must reference a different call")
        return self


class CallStartedPayload(SpecModel):
    provider: Provider
    api_family: ApiFamily = ApiFamily.unknown
    model_requested: IdStr
    model_resolved: IdStr | None = None
    service_tier: CodeStr | None = None
    inference_region: CodeStr | None = None
    stream: bool | None = None
    cache_policy: CodeStr | None = None
    prefix_fingerprint: HexStr | None = None
    fingerprint_key_id: IdStr | None = None
    prefix_tokens: NonNegInt | None = None
    prefix_token_count_method: CodeStr | None = None
    max_output_tokens: NonNegInt | None = None


class CallFinishedPayload(SpecModel):
    provider: Provider
    api_family: ApiFamily = ApiFamily.unknown
    model_requested: IdStr
    model_resolved: IdStr | None = None
    service_tier: CodeStr | None = None
    inference_region: CodeStr | None = None
    stream: bool | None = None
    status: CallStatus
    error_class: CodeStr | None = None
    provider_request_id: IdStr | None = None
    started_at_ms: UtcMs | None = Field(
        default=None,
        description="Dispatch time if the collector knows it (finish-only pipelines)",
    )
    provider_created_at_ms: UtcMs | None = Field(
        default=None,
        description="Provider-reported response creation time; evidence only, never an end time",
    )
    usage_format: UsageFormat = UsageFormat.unknown
    usage: SafeUsage | None = None
    upstream_cost_estimate_usd: DecimalStr | None = Field(
        default=None,
        description="Gateway-side estimate (e.g. LiteLLM response_cost). Never a bill.",
    )
    schema_drift: IntMap | None = Field(
        default=None,
        description="Unknown response field paths -> occurrence count. Names only, no values.",
    )
    cache_policy: CodeStr | None = None
    prefix_fingerprint: HexStr | None = None
    fingerprint_key_id: IdStr | None = None
    prefix_tokens: NonNegInt | None = None
    prefix_token_count_method: CodeStr | None = None

    @model_validator(mode="after")
    def _terminal_status(self) -> CallFinishedPayload:
        if self.status is CallStatus.in_flight:
            raise ValueError("call_finished cannot carry status in_flight")
        return self


class CallDisposition(SpecModel):
    call_id: IdStr
    disposition: Disposition
    reason: CodeStr | None = None
    removal_safe_in_scenario: bool | None = None


class OutcomePayload(SpecModel):
    status: OutcomeStatus
    success: bool | None = None
    outcome_source: OutcomeSource
    terminal_at_ms: UtcMs | None = None
    call_dispositions: list[CallDisposition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _terminal_consistency(self) -> OutcomePayload:
        terminal = (OutcomeStatus.succeeded, OutcomeStatus.failed, OutcomeStatus.abandoned)
        if self.status in terminal and self.terminal_at_ms is None:
            raise ValueError("terminal outcomes require terminal_at_ms")
        if self.status is OutcomeStatus.succeeded and self.success is False:
            raise ValueError("status succeeded contradicts success=false")
        if self.status in (OutcomeStatus.failed, OutcomeStatus.abandoned) and self.success is True:
            raise ValueError("failed/abandoned outcome contradicts success=true")
        seen: set[str] = set()
        for item in self.call_dispositions:
            if item.call_id in seen:
                raise ValueError("duplicate call_id in call_dispositions")
            seen.add(item.call_id)
        return self


_PAYLOAD_TYPES: dict[str, type[SpecModel]] = {
    EventType.call_started.value: CallStartedPayload,
    EventType.call_finished.value: CallFinishedPayload,
    EventType.outcome.value: OutcomePayload,
}


def sanitized_validation_summary(exc: ValidationError) -> str:
    """Location and error type only; never the input value or pydantic's message."""

    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}:{err.get('type', 'error')}")
    return "; ".join(parts)


class RawEnvelope(SpecModel):
    schema_version: Literal["0.1"] = SCHEMA_VERSION
    event_id: IdStr
    event_type: EventType
    revision: PosInt = 1
    dataset_id: IdStr
    data_kind: DataKind
    source_type: SourceType
    source_version: VersionStr
    observed_at_ms: UtcMs
    occurred_at_ms: UtcMs
    context: EventContext
    payload: CallStartedPayload | CallFinishedPayload | OutcomePayload

    @model_validator(mode="before")
    @classmethod
    def _typed_payload(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        event_type = data.get("event_type")
        payload = data.get("payload")
        if isinstance(event_type, EventType):
            event_type = event_type.value
        payload_type = _PAYLOAD_TYPES.get(event_type) if isinstance(event_type, str) else None
        if payload_type is not None and isinstance(payload, dict):
            data = dict(data)
            try:
                data["payload"] = payload_type.model_validate(payload)
            except ValidationError as exc:
                raise ValueError(
                    f"invalid {event_type} payload: {sanitized_validation_summary(exc)}"
                ) from None
        return data

    @model_validator(mode="after")
    def _payload_matches_event(self) -> RawEnvelope:
        expected = _PAYLOAD_TYPES[self.event_type.value]
        if not isinstance(self.payload, expected):
            raise ValueError(f"payload type does not match event_type {self.event_type.value}")
        if self.event_type in (EventType.call_started, EventType.call_finished):
            if self.context.call_id is None:
                raise ValueError("call events require context.call_id")
        if self.event_type is EventType.outcome and self.context.workflow_run_id is None:
            raise ValueError("outcome events require context.workflow_run_id")
        return self

    def content_hash(self, *, exclude: set[str] | None = None) -> str:
        """Hash of the event content.

        ``observed_at_ms`` is delivery metadata, not content, so a re-delivered callback
        with the same body is a duplicate rather than a conflict.
        """

        excluded = {"observed_at_ms"} | (exclude or set())
        return super().content_hash(exclude=excluded)

    def body_hash(self) -> str:
        """Hash of the semantic body: also ignores ``event_id``.

        Two distinct events with the same body describe the same fact (a repeated
        terminal state); same event_id with a different body is a conflict.
        """

        return self.content_hash(exclude={"event_id"})

    @property
    def target_id(self) -> str:
        """The projection this event updates: call_id for calls, run id for outcomes."""

        if self.event_type is EventType.outcome:
            assert self.context.workflow_run_id is not None
            return self.context.workflow_run_id
        assert self.context.call_id is not None
        return self.context.call_id
