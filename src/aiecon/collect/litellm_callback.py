"""Per-attempt LiteLLM collection (PLAN.md §5.1, source S5).

Two layers:

* :class:`EnvelopeCollector` is LiteLLM-agnostic and fully testable: it turns the per-attempt
  deployment hook arguments (plain dicts / objects) into allowlisted ``RawEnvelope`` records.
* :class:`AieconLiteLLMLogger` is the thin ``CustomLogger`` subclass registered in the proxy
  config. It only forwards the three per-attempt deployment hooks. Request-level hooks are
  deliberately not used because they fire once per logical request (S5).

Every attempt gets its own ``call_id`` in ``async_pre_call_deployment_hook``. The id is
attached to *that attempt's* request data (a copied metadata dict, never a shared one) so the
success/failure hook can find it. Lineage is derived from the application-supplied
``node_run_id``: a later attempt for the same node run is a retry when the model group is
unchanged and a fallback otherwise. Nothing is inferred from timing.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

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

PROVIDER_MAP: dict[str, Provider] = {
    "openai": Provider.openai,
    "azure": Provider.other,
    "anthropic": Provider.anthropic,
}

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

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _litellm_params(kwargs: Mapping[str, Any]) -> Mapping[str, Any]:
        params = kwargs.get("litellm_params")
        return params if isinstance(params, Mapping) else {}

    @classmethod
    def _app_meta(cls, kwargs: Mapping[str, Any]) -> Mapping[str, Any]:
        for container in (cls._litellm_params(kwargs).get("metadata"), kwargs.get("metadata")):
            if isinstance(container, Mapping):
                meta = container.get("aiecon")
                if isinstance(meta, Mapping):
                    return meta
        return {}

    @classmethod
    def call_id_from(cls, request_data: Mapping[str, Any]) -> str | None:
        for container in (
            cls._litellm_params(request_data).get("metadata"),
            request_data.get("metadata"),
        ):
            if isinstance(container, Mapping):
                found = _safe_id(container.get("aiecon_call_id"))
                if found:
                    return found
        return None

    def _provider(self, kwargs: Mapping[str, Any]) -> Provider:
        raw = self._litellm_params(kwargs).get("custom_llm_provider") or kwargs.get(
            "custom_llm_provider"
        )
        if isinstance(raw, str) and raw in PROVIDER_MAP:
            return PROVIDER_MAP[raw]
        model = kwargs.get("model")
        if isinstance(model, str) and "/" in model:
            prefix = model.split("/", 1)[0]
            return PROVIDER_MAP.get(prefix, Provider.other)
        return Provider.other

    def _context(self, meta: Mapping[str, Any], call_id: str, attempt_index: int) -> EventContext:
        return EventContext(
            workflow_id=_safe_id(meta.get("workflow_id")),
            workflow_run_id=_safe_id(meta.get("workflow_run_id")),
            node_id=_safe_id(meta.get("node_id")),
            node_run_id=_safe_id(meta.get("node_run_id")),
            call_id=call_id,
            scope_id=_safe_id(meta.get("scope_id")) or self.config.scope_id,
            attempt_index=attempt_index,
            trace_id=_safe_id(meta.get("trace_id")),
            span_id=_safe_id(meta.get("span_id")),
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
        model_group = _safe_id(self._litellm_params(kwargs).get("model_group")) or model_requested
        provider = self._provider(kwargs)
        api_family = API_FAMILY_BY_PROVIDER.get(provider, ApiFamily.unknown)

        node_run_id = _safe_id(meta.get("node_run_id"))
        history = self._node_history.get(node_run_id, []) if node_run_id else []
        attempt_index = len(history) + 1
        context = self._context(meta, call_id, attempt_index)
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

        # attach the id to a *copy* of this attempt's metadata; never mutate shared dicts
        params = dict(self._litellm_params(kwargs))
        metadata = (
            dict(params.get("metadata") or {})
            if isinstance(params.get("metadata"), Mapping)
            else {}
        )
        metadata["aiecon_call_id"] = call_id
        params["metadata"] = metadata
        new_kwargs = dict(kwargs)
        new_kwargs["litellm_params"] = params
        return new_kwargs

    def _finish(
        self,
        request_data: Mapping[str, Any],
        *,
        status: CallStatus,
        error_class: str | None,
        response: Any,
    ) -> bool:
        call_id = self.call_id_from(request_data)
        # attempts stay in memory (pruned by age) so a redelivered hook rebuilds the same body
        attempt = self._attempts.get(call_id) if call_id else None
        created = _safe_int(_get(response, "created")) if response is not None else None
        if created is not None and 1_000_000_000 < created < 4_102_444_800:
            ts = created * 1000  # provider-reported creation time: stable across redelivery
        else:
            ts = self.clock()
        if call_id is None:
            # finish without a matching start: still record the attempt, as its own call
            call_id = f"call_{uuid.uuid4().hex}"
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
            provider = self._provider(request_data)
            api_family = API_FAMILY_BY_PROVIDER.get(provider, ApiFamily.unknown)
            model_requested = _safe_id(request_data.get("model")) or "unknown-model"
            context = self._context(meta, call_id, 1)
            started = None

        safe_usage: dict[str, Any] | None = None
        drift: dict[str, int] | None = None
        model_resolved = None
        upstream_cost = None
        provider_request_id = None
        if response is not None:
            usage_obj = _get(response, "usage")
            if usage_obj is not None:
                safe_usage, drift_map = build_safe_usage(usage_obj, LITELLM_ALLOWLIST)
                drift = drift_map or None
                if not safe_usage:
                    safe_usage = None
            model_resolved = _safe_id(_get(response, "model"))
            provider_request_id = _safe_id(_get(response, "id"))
            hidden = _get(response, "_hidden_params")
            upstream_cost = _safe_decimal(_get(hidden, "response_cost")) if hidden else None
        if upstream_cost is None:
            upstream_cost = _safe_decimal(request_data.get("response_cost"))

        payload = CallFinishedPayload(
            provider=provider,
            api_family=api_family,
            model_requested=model_requested,
            model_resolved=model_resolved,
            stream=bool(request_data.get("stream"))
            if request_data.get("stream") is not None
            else None,
            status=status,
            error_class=error_class,
            provider_request_id=provider_request_id,
            started_at_ms=started,
            usage_format=UsageFormat.litellm_standard if safe_usage else UsageFormat.unknown,
            usage=safe_usage,
            upstream_cost_estimate_usd=upstream_cost,
            schema_drift=drift,
            **self._prefix_fields(meta),
        )
        return self.writer.write(
            self._envelope(
                f"ev_{call_id}_finished_1", EventType.call_finished, ts, context, payload
            )
        )

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
        return self._finish(request_data, status=status, error_class=error_class, response=None)

    def health(self) -> dict[str, Any]:
        return {
            **self.writer.health(),
            "open_attempts": len(self._attempts),
            "hook_errors": self.hook_errors,
            "last_hook_error_class": self.last_hook_error_class,
        }

    def _guard(self, fn: Callable[[], Any], fallback: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - hooks must never break the model path
            self.hook_errors += 1
            self.last_hook_error_class = type(exc).__name__
            return fallback


class AieconLiteLLMLogger(CustomLogger):
    """LiteLLM ``CustomLogger`` forwarding the three per-attempt deployment hooks.

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
            import litellm

            version = f"litellm=={getattr(litellm, '__version__', 'unknown')}"
        except ImportError:  # pragma: no cover
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
