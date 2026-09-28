"""Shared result type for usage adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aiecon.spec.call import UsageCompleteness
from aiecon.spec.common import Provider


@dataclass
class NormalizedUsage:
    """Mutually exclusive token classes. ``None`` means unknown, never zero."""

    input_total: int | None = None
    input_uncached: int | None = None
    input_cache_read: int | None = None
    input_cache_write: int | None = None
    output: int | None = None
    cache_write_breakdown: dict[str, int] | None = None
    completeness: UsageCompleteness = UsageCompleteness.missing
    notes: list[str] = field(default_factory=list)

    def note(self, code: str) -> None:
        if code not in self.notes:
            self.notes.append(code)


PROVIDER_MAP: dict[str, Provider] = {
    "openai": Provider.openai,
    "anthropic": Provider.anthropic,
}

# Bare model ids that gateways route without a provider prefix. Only unambiguous families.
MODEL_NAME_HINTS: tuple[tuple[str, Provider], ...] = (
    ("gpt-", Provider.openai),
    ("chatgpt-", Provider.openai),
    ("o1", Provider.openai),
    ("o3", Provider.openai),
    ("o4", Provider.openai),
    ("claude-", Provider.anthropic),
)


def model_family(model: str | None) -> Provider | None:
    """Provider implied by a model id without any network or gateway lookup.

    An explicit ``prefix/`` maps through PROVIDER_MAP (unknown prefixes are ``other``);
    otherwise the known bare families decide; anything else is ``None`` (unknown).
    """

    if not isinstance(model, str) or not model:
        return None
    if "/" in model:
        return PROVIDER_MAP.get(model.split("/", 1)[0].lower(), Provider.other)
    lowered = model.lower()
    for hint, provider in MODEL_NAME_HINTS:
        if lowered.startswith(hint):
            return provider
    return None


def resolved_model_or_none(
    provider: Provider, model_requested: str | None, model_resolved: str | None
) -> str | None:
    """Keep a provider-reported model id only when it can be one.

    Seen live: LiteLLM reports its own model group (route name such as ``anthropic-live``)
    as the model of a streamed Anthropic response. A resolved id whose family belongs to
    another provider, or that has no known family while the requested id has one, is not
    a provider model id and is recorded as unknown so ``model_requested`` is used.
    """

    if model_resolved is None:
        return None
    implied = model_family(model_resolved)
    if implied is not None:
        return model_resolved if implied is provider else None
    return model_resolved if model_family(model_requested) is None else None


KNOWN_INFERENCE_REGIONS = frozenset({"global", "us"})
"""Region codes that select prices. Anything else (Anthropic answers ``not_available`` for
default routing) is recorded as unknown so the estimator applies the default global price."""


def known_region(value: str | None) -> str | None:
    return value if value in KNOWN_INFERENCE_REGIONS else None


def get_int(usage: Mapping[str, Any] | None, *path: str) -> int | None:
    """Fetch a nested non-negative integer; anything else is ``None``."""

    node: Any = usage
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    if isinstance(node, bool) or not isinstance(node, int) or node < 0:
        return None
    return node


def has_key(usage: Mapping[str, Any] | None, *path: str) -> bool:
    node: Any = usage
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return False
        node = node[key]
    return True


def finish(result: NormalizedUsage) -> NormalizedUsage:
    """Derive completeness from which fields are known and internally consistent."""

    if result.completeness is UsageCompleteness.invalid:
        return result
    core = (result.input_total, result.input_uncached, result.input_cache_read, result.output)
    if all(v is None for v in core) and result.input_cache_write is None:
        result.completeness = UsageCompleteness.missing
        return result
    known_inputs = (result.input_uncached, result.input_cache_read, result.input_cache_write)
    if result.input_total is not None and all(v is not None for v in known_inputs):
        if sum(v for v in known_inputs if v is not None) != result.input_total:
            result.completeness = UsageCompleteness.invalid
            result.note("input_parts_do_not_sum_to_total")
            return result
    if result.cache_write_breakdown is not None and result.input_cache_write is not None:
        if sum(result.cache_write_breakdown.values()) != result.input_cache_write:
            result.completeness = UsageCompleteness.invalid
            result.note("cache_write_breakdown_mismatch")
            return result
    complete = result.output is not None and all(v is not None for v in known_inputs)
    result.completeness = UsageCompleteness.complete if complete else UsageCompleteness.partial
    return result
