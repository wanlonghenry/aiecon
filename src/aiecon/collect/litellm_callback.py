"""Per-attempt LiteLLM collection (PLAN.md §5.1, source S5).

Two layers:

* :class:`EnvelopeCollector` is LiteLLM-agnostic and fully testable: it turns the hook
  arguments (plain dicts / objects) into allowlisted ``RawEnvelope`` records.
* :class:`AieconLiteLLMLogger` is the thin ``CustomLogger`` subclass registered in the proxy
  config.

Which hooks carry which fact (probed against LiteLLM 1.102.1 on 2026-09-27, via ``Router``
with mock responses and via the proxy):

* ``async_pre_call_deployment_hook`` fires once per physical attempt with the bare deployment
  ``model``, ``max_tokens``, ``stream``, ``litellm_call_id``, ``litellm_trace_id``,
  ``api_key``, ``messages`` and a ``metadata`` dict (``model_group``, ``deployment``, ...).
  aiecon allocates the attempt ``call_id`` here and writes ``call_started``.
* ``async_post_call_success_deployment_hook`` fires for non-streaming attempts only, with the
  ``ModelResponse``. ``async_post_call_failure_deployment_hook`` fires for failed attempts.
* For **streaming** attempts no deployment success hook fires. The terminal state comes from
  ``async_log_success_event`` (once the stream has been consumed, with the rebuilt response
  and its final usage) or ``async_log_failure_event``. Both carry our ``aiecon_call_id`` in
  ``litellm_params.metadata``. A stream the client abandons produces no terminal event at
  all: the call stays ``in_flight`` with unknown cost, by design.

The first terminal notification for an attempt wins; later ones for the same attempt (the
log event after a deployment hook, a redelivery) are counted and ignored, so a call never
gets two terminal envelopes. ``ended_at_ms`` is the local clock at the first terminal;
the provider's ``created`` timestamp is kept separately and never used as an end time.

Only the named fields are ever read; ``api_key``, ``messages`` and everything else are
never touched. Lineage is derived from the application-supplied ``node_run_id``: a later
attempt for the same node run is a retry when the model group is unchanged and a fallback
otherwise. Nothing is inferred from timing.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from aiecon.adapters.base import PROVIDER_MAP, known_region, model_family
from aiecon.adapters.litellm import LITELLM_ALLOWLIST
from aiecon.collect.writer import JsonlWriter
from aiecon.config import now_ms
from aiecon.privacy import build_safe_usage, classify_exception
from aiecon.spec.common import CODE_PATTERN, ID_PATTERN, ApiFamily, DataKind, Provider
from aiecon.spec.envelope import (
    CallFinishedPayload,
    CallStartedPayload,
    CallStatus,
    EventContext,
    EventType,
    RawEnvelope,
    SourceType,
    UsageFormat,
)

try:  # pragma: no cover - exercised only with the live extra installed
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover

    class CustomLogger:  # type: ignore[no-redef]
        """Stand-in so the module imports without LiteLLM."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass


_ID_RE = re.compile(ID_PATTERN)
_CODE_RE = re.compile(CODE_PATTERN)
_HEX_RE = re.compile(r"^[0-9a-f]{16,128}$")

API_FAMILY_BY_PROVIDER: dict[Provider, ApiFamily] = {
    Provider.openai: ApiFamily.chat_completions,
    Provider.anthropic: ApiFamily.messages,
}

ATTEMPT_MEMORY_MS = 60 * 60 * 1000


def _safe_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID_RE.fullmatch(value) else None


def _safe_code(value: Any) -> str | None:
    return value if isinstance(value, str) and _CODE_RE.fullmatch(value) else None


def _safe_hex(value: Any) -> str | None:
    return value if isinstance(value, str) and _HEX_RE.fullmatch(value) else None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _safe_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float | Decimal):
        try:
            result = Decimal(str(value))
        except Exception:  # noqa: BLE001
            return None
        return result if result.is_finite() and result >= 0 else None
    return None


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def provider_from_model_name(model: str | None) -> Provider | None:
    """Provider implied by a model id: explicit ``prefix/`` first, then known families."""

    if not isinstance(model, str) or not model:
        return None
    family = model_family(model)
    if family is not None:
        return family
    try:  # LiteLLM knows far more families; use it when it is importable
        from litellm import get_llm_provider

        _model, provider_name, _key, _base = get_llm_provider(model=model)
    except Exception:  # noqa: BLE001 - unknown model or LiteLLM absent
        return None
    if isinstance(provider_name, str):
        return PROVIDER_MAP.get(provider_name.lower(), Provider.other)
    return None


@dataclass
class CollectorConfig:
    dataset_id: str
    data_kind: DataKind = DataKind.live
    scope_id: str = "litellm_proxy"
    source_version: str = "litellm==unknown"


@dataclass
class _Attempt:
    call_id: str
    started_at_ms: int
    provider: Provider
    api_family: ApiFamily
    model_requested: str
    model_group: str
    context: EventContext
    started_written: bool = True
    terminal_written: bool = False
    finished_at_ms: int | None = None


@dataclass
class EnvelopeCollector:
    writer: JsonlWriter
    config: CollectorConfig
    clock: Callable[[], int] = now_ms
    _attempts: dict[str, _Attempt] = field(default_factory=dict)
    _node_history: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    _used_ids: set[str] = field(default_factory=set)
    hook_errors: int = 0
    last_hook_error_class: str | None = None
    terminal_duplicates_ignored: int = 0
    terminals_without_start: int = 0

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _litellm_params(kwargs: Mapping[str, Any]) -> Mapping[str, Any]:
        return _mapping(kwargs.get("litellm_params"))

    @classmethod
    def _metadata_views(cls, kwargs: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        """Every place LiteLLM may keep metadata, most specific first."""

        views = [kwargs.get("metadata"), cls._litellm_params(kwargs).get("metadata")]
        return [v for v in views if isinstance(v, Mapping)]

    @classmethod
    def _app_meta(cls, kwargs: Mapping[str, Any]) -> Mapping[str, Any]:
        for container in cls._metadata_views(kwargs):
            meta = container.get("aiecon")
            if isinstance(meta, Mapping):
                return meta
        return {}

    @classmethod
    def call_id_from(cls, request_data: Mapping[str, Any]) -> str | None:
        for container in cls._metadata_views(request_data):
            found = _safe_id(container.get("aiecon_call_id"))
            if found:
                return found
        return None

    @classmethod
    def _model_group(cls, kwargs: Mapping[str, Any]) -> str | None:
        for container in cls._metadata_views(kwargs):
            group = _safe_id(container.get("model_group"))
            if group:
                return group
        return _safe_id(cls._litellm_params(kwargs).get("model_group"))

    def _provider(self, kwargs: Mapping[str, Any], *, hint: Any = None) -> Provider:
        candidates: list[Any] = [
            hint,
            kwargs.get("custom_llm_provider"),
            self._litellm_params(kwargs).get("custom_llm_provider"),
        ]
        for container in self._metadata_views(kwargs):
            deployment = _mapping(container.get("deployment"))
            candidates.append(_mapping(deployment.get("litellm_params")).get("custom_llm_provider"))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate:
                return PROVIDER_MAP.get(candidate.lower(), Provider.other)
        return provider_from_model_name(kwargs.get("model")) or Provider.other

    def _context(
        self,
        meta: Mapping[str, Any],
        kwargs: Mapping[str, Any],
        call_id: str,
        attempt_index: int,
    ) -> EventContext:
        return EventContext(
            workflow_id=_safe_id(meta.get("workflow_id")),
            workflow_run_id=_safe_id(meta.get("workflow_run_id")),
            node_id=_safe_id(meta.get("node_id")),
            node_run_id=_safe_id(meta.get("node_run_id")),
            call_id=call_id,
            scope_id=_safe_id(meta.get("scope_id")) or self.config.scope_id,
            attempt_index=attempt_index,
            trace_id=_safe_id(meta.get("trace_id")) or _safe_id(kwargs.get("litellm_trace_id")),
            span_id=_safe_id(meta.get("span_id")) or _safe_id(kwargs.get("litellm_call_id")),
        )

    def _prefix_fields(self, meta: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "cache_policy": _safe_code(meta.get("cache_policy")),
            "prefix_fingerprint": _safe_hex(meta.get("prefix_fingerprint")),
            "fingerprint_key_id": _safe_id(meta.get("fingerprint_key_id")),
            "prefix_tokens": _safe_int(meta.get("prefix_tokens")),
            "prefix_token_count_method": _safe_code(meta.get("prefix_token_count_method")),
        }

    def _envelope(
        self, event_id: str, event_type: EventType, ts: int, context: EventContext, payload: Any
    ) -> RawEnvelope:
        return RawEnvelope(
            event_id=event_id,
            event_type=event_type,
            revision=1,
            dataset_id=self.config.dataset_id,
            data_kind=self.config.data_kind,
            source_type=SourceType.litellm_callback,
            source_version=self.config.source_version,
            observed_at_ms=self.clock(),
            occurred_at_ms=ts,
            context=context,
            payload=payload,
        )

    def _prune_history(self, now: int) -> None:
        stale = [
            k
            for k, items in self._node_history.items()
            if items and now - items[-1][1] > ATTEMPT_MEMORY_MS
        ]
        for key in stale:
            del self._node_history[key]
        old_attempts = [
            k for k, a in self._attempts.items() if now - a.started_at_ms > ATTEMPT_MEMORY_MS
        ]
        for key in old_attempts:
            del self._attempts[key]

    # -------------------------------------------------------------- hooks
    def begin_attempt(self, kwargs: Mapping[str, Any], call_type: str) -> dict[str, Any]:
        """Allocate the attempt id, write ``call_started`` and return the attempt's kwargs."""

        ts = self.clock()
        self._prune_history(ts)
        meta = self._app_meta(kwargs)
        # An application may name the attempt itself (explicit client-driven retries), so it
        # can label dispositions later. A name that was already used gets a fresh id instead
        # of silently merging two attempts.
        requested_id = _safe_id(meta.get("call_id"))
        if requested_id and requested_id not in self._used_ids:
            call_id = requested_id
        else:
            call_id = f"call_{uuid.uuid4().hex}"
        self._used_ids.add(call_id)
        if len(self._used_ids) > 100_000:
            self._used_ids.clear()
        model_requested = _safe_id(kwargs.get("model")) or "unknown-model"
        model_group = self._model_group(kwargs) or model_requested
        provider = self._provider(kwargs)
        api_family = API_FAMILY_BY_PROVIDER.get(provider, ApiFamily.unknown)

        node_run_id = _safe_id(meta.get("node_run_id"))
        history = self._node_history.get(node_run_id, []) if node_run_id else []
        attempt_index = len(history) + 1
        context = self._context(meta, kwargs, call_id, attempt_index)
        if history:
            previous_id = history[-1][0]
            previous = self._attempts.get(previous_id)
            same_group = previous is not None and previous.model_group == model_group
            if same_group:
                context = context.model_copy(update={"retry_of_call_id": previous_id})
            else:
                context = context.model_copy(update={"fallback_of_call_id": previous_id})

        payload = CallStartedPayload(
            provider=provider,
            api_family=api_family,
            model_requested=model_requested,
            stream=bool(kwargs.get("stream")) if kwargs.get("stream") is not None else None,
            max_output_tokens=_safe_int(kwargs.get("max_tokens")),
            **self._prefix_fields(meta),
        )
        written = self.writer.write(
            self._envelope(f"ev_{call_id}_started", EventType.call_started, ts, context, payload)
        )
        self._attempts[call_id] = _Attempt(
            call_id=call_id,
            started_at_ms=ts,
            provider=provider,
            api_family=api_family,
            model_requested=model_requested,
            model_group=model_group,
            context=context,
            started_written=written,
        )
        if node_run_id:
            self._node_history.setdefault(node_run_id, []).append((call_id, ts))

        # attach the id to *copies* of this attempt's metadata; never mutate shared dicts
        new_kwargs = dict(kwargs)
        metadata = dict(_mapping(kwargs.get("metadata")))
        metadata["aiecon_call_id"] = call_id
        new_kwargs["metadata"] = metadata
        if isinstance(kwargs.get("litellm_params"), Mapping):
            params = dict(self._litellm_params(kwargs))
            inner = dict(_mapping(params.get("metadata")))
            inner["aiecon_call_id"] = call_id
            params["metadata"] = inner
            new_kwargs["litellm_params"] = params
        return new_kwargs

    def _finish(
        self,
        request_data: Mapping[str, Any],
        *,
        status: CallStatus,
        error_class: str | None,
        response: Any,
        provider_hint: Any = None,
    ) -> bool:
        call_id = self.call_id_from(request_data)
        attempt = self._attempts.get(call_id) if call_id else None
        if attempt is not None and attempt.terminal_written:
            # a second terminal notification for the same attempt (log event after the
            # deployment hook, or a redelivery): the first terminal state stands
            self.terminal_duplicates_ignored += 1
            return True
        if attempt is not None and attempt.finished_at_ms is not None:
            ts = attempt.finished_at_ms
        else:
            ts = self.clock()
        if call_id is None:
            # finish without a matching start: still record the attempt, as its own call
            call_id = f"call_{uuid.uuid4().hex}"
            self.terminals_without_start += 1
        meta = self._app_meta(request_data)
        if attempt is not None:
            context = attempt.context
            provider, api_family, model_requested = (
                attempt.provider,
                attempt.api_family,
                attempt.model_requested,
            )
            started = attempt.started_at_ms
        else:
            provider = self._provider(request_data, hint=provider_hint)
            api_family = API_FAMILY_BY_PROVIDER.get(provider, ApiFamily.unknown)
            model_requested = _safe_id(request_data.get("model")) or "unknown-model"
            context = self._context(meta, request_data, call_id, 1)
            started = None
        if provider is Provider.other and isinstance(provider_hint, str):
            provider = PROVIDER_MAP.get(provider_hint.lower(), Provider.other)
            api_family = API_FAMILY_BY_PROVIDER.get(provider, ApiFamily.unknown)

        safe_usage: dict[str, Any] | None = None
        drift: dict[str, int] | None = None
        model_resolved = None
        upstream_cost = None
        provider_request_id = None
        provider_created_at_ms = None
        service_tier = None
        inference_region = None
        if response is not None:
            usage_obj = _get(response, "usage")
            if usage_obj is not None:
                safe_usage, drift_map = build_safe_usage(usage_obj, LITELLM_ALLOWLIST)
                drift = drift_map or None
                # the envelope allows two levels of usage nesting; LiteLLM 1.102.1 puts the
                # Anthropic 5m/1h breakdown three levels deep, so hoist it to the top-level
                # key the adapter has always read
                details = _mapping(safe_usage.get("prompt_tokens_details"))
                nested = details.get("cache_creation_token_details")
                if isinstance(nested, Mapping):
                    safe_usage["prompt_tokens_details"] = {
                        k: v for k, v in details.items() if k != "cache_creation_token_details"
                    }
                    safe_usage.setdefault("cache_creation_token_details", dict(nested))
                if not safe_usage:
                    safe_usage = None
                # LiteLLM adds these two short codes to the usage object on some paths
                # (seen live for Anthropic); they select prices, so keep them as codes
                service_tier = _safe_code(_get(usage_obj, "service_tier"))
                # Anthropic answers "not_available" when no region was pinned: that is the
                # default (global) routing, not a region, so only known codes are kept
                inference_region = known_region(_safe_code(_get(usage_obj, "inference_geo")))
                if drift:
                    for key, value in (
                        ("service_tier", service_tier),
                        ("inference_geo", inference_region),
                    ):
                        if value is not None:
                            drift.pop(key, None)  # captured above, so not unknown
                    drift = drift or None
            model_resolved = _safe_id(_get(response, "model"))
            # streamed Anthropic responses carry the LiteLLM model group (route name) here
            # instead of a provider model id (seen live); a route name is never a model
            if model_resolved is not None and model_resolved == self._model_group(request_data):
                model_resolved = None
            provider_request_id = _safe_id(_get(response, "id"))
            created = _safe_int(_get(response, "created"))
            if created is not None and 1_000_000_000 < created < 4_102_444_800:
                provider_created_at_ms = created * 1000
            hidden = _get(response, "_hidden_params")
            upstream_cost = _safe_decimal(_get(hidden, "response_cost")) if hidden else None
            hidden_provider = _get(hidden, "custom_llm_provider") if hidden else None
            if provider is Provider.other and isinstance(hidden_provider, str):
                provider = PROVIDER_MAP.get(hidden_provider.lower(), Provider.other)
                api_family = API_FAMILY_BY_PROVIDER.get(provider, ApiFamily.unknown)
        if upstream_cost is None:
            upstream_cost = _safe_decimal(request_data.get("response_cost"))

        stream_flag = request_data.get("stream")
        payload = CallFinishedPayload(
            provider=provider,
            api_family=api_family,
            model_requested=model_requested,
            model_resolved=model_resolved,
            stream=bool(stream_flag) if stream_flag is not None else None,
            status=status,
            error_class=error_class,
            provider_request_id=provider_request_id,
            service_tier=service_tier,
            inference_region=inference_region,
            started_at_ms=started,
            provider_created_at_ms=provider_created_at_ms,
            usage_format=UsageFormat.litellm_standard if safe_usage else UsageFormat.unknown,
            usage=safe_usage,
            upstream_cost_estimate_usd=upstream_cost,
            schema_drift=drift,
            **self._prefix_fields(meta),
        )
        written = self.writer.write(
            self._envelope(
                f"ev_{call_id}_finished_1", EventType.call_finished, ts, context, payload
            )
        )
        if attempt is not None:
            attempt.finished_at_ms = ts
            attempt.terminal_written = written
        return written

    def finish_success(
        self, request_data: Mapping[str, Any], response: Any, call_type: str
    ) -> bool:
        return self._finish(
            request_data, status=CallStatus.success, error_class=None, response=response
        )

    def finish_failure(
        self,
        request_data: Mapping[str, Any],
        exception: BaseException,
        call_type: str,
        fallback_depth: int | None = None,
    ) -> bool:
        error_class = classify_exception(exception)
        if error_class == "timeout":
            status = CallStatus.timeout
        elif error_class == "cancelled":
            status = CallStatus.cancelled
        else:
            status = CallStatus.error
        return self._finish(
            request_data,
            status=status,
            error_class=error_class,
            response=None,
            provider_hint=getattr(exception, "llm_provider", None),
        )

    def finish_from_log_event(
        self, kwargs: Mapping[str, Any], response_obj: Any, *, success: bool
    ) -> bool:
        """Terminal state from the request-level log event.

        This is the only terminal source for streaming attempts (no deployment success hook
        fires for streams). For non-streaming attempts it arrives after the deployment hook
        and is ignored as a duplicate.
        """

        if success:
            return self.finish_success(kwargs, response_obj, "log_event")
        exception = kwargs.get("exception")
        if not isinstance(exception, BaseException):
            exception = RuntimeError("unknown failure")
        return self.finish_failure(kwargs, exception, "log_event")

    def health(self) -> dict[str, Any]:
        return {
            **self.writer.health(),
            "open_attempts": sum(1 for a in self._attempts.values() if not a.terminal_written),
            "hook_errors": self.hook_errors,
            "last_hook_error_class": self.last_hook_error_class,
            "terminal_duplicates_ignored": self.terminal_duplicates_ignored,
            "terminals_without_start": self.terminals_without_start,
        }

    def _guard(self, fn: Callable[[], Any], fallback: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - hooks must never break the model path
            self.hook_errors += 1
            self.last_hook_error_class = type(exc).__name__
            return fallback


class AieconLiteLLMLogger(CustomLogger):
    """LiteLLM ``CustomLogger``: per-attempt deployment hooks plus the log events that carry
    the terminal state of streaming attempts.

    Proxy config: ``litellm_settings.callbacks: custom_callbacks.proxy_handler_instance``.
    """

    def __init__(self, collector: EnvelopeCollector):
        super().__init__()
        self.collector = collector

    @classmethod
    def from_env(cls) -> AieconLiteLLMLogger:
        import os

        from aiecon.config import resolve_workspace
        from aiecon.storage import Workspace

        try:
            from importlib.metadata import version as _dist_version

            version = f"litellm=={_dist_version('litellm')}"
        except Exception:  # noqa: BLE001 - version is informational only
            version = "litellm==unknown"
        workspace = Workspace(resolve_workspace(None))
        writer = JsonlWriter(workspace.raw_dir)
        config = CollectorConfig(
            dataset_id=_safe_id(os.environ.get("AIECON_DATASET_ID")) or "live-demo",
            data_kind=DataKind.live,
            scope_id=_safe_id(os.environ.get("AIECON_SCOPE_ID")) or "litellm_proxy",
            source_version=version,
        )
        return cls(EnvelopeCollector(writer=writer, config=config))

    # per-attempt deployment hooks -------------------------------------------------
    async def async_pre_call_deployment_hook(
        self, kwargs: dict[str, Any], call_type: Any
    ) -> dict[str, Any]:
        return self.collector._guard(
            lambda: self.collector.begin_attempt(kwargs, str(call_type)), kwargs
        )

    async def async_post_call_success_deployment_hook(
        self, request_data: dict[str, Any], response: Any, call_type: Any
    ) -> None:
        self.collector._guard(
            lambda: self.collector.finish_success(request_data, response, str(call_type)), None
        )

    async def async_post_call_failure_deployment_hook(
        self,
        request_data: dict[str, Any],
        exception: Exception,
        call_type: Any,
        fallback_depth: int | None = None,
    ) -> None:
        self.collector._guard(
            lambda: self.collector.finish_failure(
                request_data, exception, str(call_type), fallback_depth
            ),
            None,
        )

    # request-level log events: terminal state for streams --------------------------
    async def async_log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.collector._guard(
            lambda: self.collector.finish_from_log_event(kwargs, response_obj, success=True),
            None,
        )

    async def async_log_failure_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.collector._guard(
            lambda: self.collector.finish_from_log_event(kwargs, response_obj, success=False),
            None,
        )

    def log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.collector._guard(
            lambda: self.collector.finish_from_log_event(kwargs, response_obj, success=True),
            None,
        )

    def log_failure_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.collector._guard(
            lambda: self.collector.finish_from_log_event(kwargs, response_obj, success=False),
            None,
        )
