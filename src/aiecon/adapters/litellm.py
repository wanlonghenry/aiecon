"""LiteLLM's transformed usage object (PLAN.md §5.1, source S5).

LiteLLM converts every provider response into an OpenAI-shaped ``Usage`` object and adds
provider extras. The dangerous case is Anthropic: LiteLLM already folds cache tokens into
``prompt_tokens`` and exposes ``cache_creation_input_tokens`` / ``cache_read_input_tokens``
next to it. Re-applying the native Anthropic rule (add the three) would double count, so this
adapter treats ``prompt_tokens`` as the *total* and subtracts the cache classes.

The exact field layout is pinned to the LiteLLM version recorded in
docs/provider-assumptions.md and covered by fixtures captured from that version.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiecon.adapters.base import NormalizedUsage, finish, get_int, has_key
from aiecon.spec.call import UsageCompleteness

LITELLM_ALLOWLIST: Mapping[str, Any] = {
    "prompt_tokens": True,
    "completion_tokens": True,
    "total_tokens": True,
    "prompt_tokens_details": {
        "cached_tokens": True,
        "audio_tokens": True,
        "text_tokens": True,
        "image_tokens": True,
        "cache_creation_tokens": True,
    },
    "completion_tokens_details": {
        "reasoning_tokens": True,
        "audio_tokens": True,
        "text_tokens": True,
        "accepted_prediction_tokens": True,
        "rejected_prediction_tokens": True,
    },
    "cache_creation_input_tokens": True,
    "cache_read_input_tokens": True,
    "cache_creation_token_details": {
        "ephemeral_5m_input_tokens": True,
        "ephemeral_1h_input_tokens": True,
    },
}

WRITE_TIERS: tuple[tuple[str, str], ...] = (
    ("ephemeral_5m_input_tokens", "ephemeral_5m"),
    ("ephemeral_1h_input_tokens", "ephemeral_1h"),
)


def normalize_litellm(usage: Mapping[str, Any] | None) -> NormalizedUsage:
    result = NormalizedUsage()
    if usage is None:
        return finish(result)
    result.input_total = get_int(usage, "prompt_tokens")
    result.output = get_int(usage, "completion_tokens")

    read = None
    if has_key(usage, "cache_read_input_tokens"):
        read = get_int(usage, "cache_read_input_tokens")
    if read is None and has_key(usage, "prompt_tokens_details", "cached_tokens"):
        read = get_int(usage, "prompt_tokens_details", "cached_tokens")
    if read is None:
        result.note("cache_read_field_absent")
    result.input_cache_read = read

    write = None
    if has_key(usage, "cache_creation_input_tokens"):
        write = get_int(usage, "cache_creation_input_tokens")
    if write is None and has_key(usage, "prompt_tokens_details", "cache_creation_tokens"):
        write = get_int(usage, "prompt_tokens_details", "cache_creation_tokens")
    if write is None:
        write = 0
        result.note("cache_write_absent_treated_as_zero")
    result.input_cache_write = write

    if has_key(usage, "cache_creation_token_details"):
        breakdown: dict[str, int] = {}
        for source_key, tier in WRITE_TIERS:
            value = get_int(usage, "cache_creation_token_details", source_key)
            if value is not None:
                breakdown[tier] = value
        if breakdown:
            if sum(breakdown.values()) != write:
                result.completeness = UsageCompleteness.invalid
                result.note("cache_write_breakdown_mismatch")
                result.cache_write_breakdown = breakdown
                return result
            result.cache_write_breakdown = breakdown
    elif write > 0:
        result.note("cache_write_tier_unknown")

    if result.input_total is not None and read is not None:
        uncached = result.input_total - read - write
        if uncached < 0:
            result.completeness = UsageCompleteness.invalid
            result.note("cached_exceeds_total")
            return result
        result.input_uncached = uncached
    return finish(result)
