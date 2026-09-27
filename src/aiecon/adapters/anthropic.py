"""Anthropic Messages usage format (PLAN.md §6.3, source S4).

Contract encoded here (see docs/provider-assumptions.md for the verified references):

* ``input_tokens`` is the *uncached* remainder only. Total input is
  ``input_tokens + cache_creation_input_tokens + cache_read_input_tokens``.
* ``cache_creation`` breaks the write total into ``ephemeral_5m_input_tokens`` and
  ``ephemeral_1h_input_tokens``. When both the total and the breakdown are present the
  breakdown is the priced quantity and the total is only checked for consistency.
* A write total without a breakdown cannot be priced without guessing the TTL tier, so the
  normalizer keeps the total and leaves ``cache_write_breakdown`` empty; the estimator then
  reports that portion as unpriced.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiecon.adapters.base import NormalizedUsage, finish, get_int, has_key
from aiecon.spec.call import UsageCompleteness

MESSAGES_ALLOWLIST: Mapping[str, Any] = {
    "input_tokens": True,
    "output_tokens": True,
    "cache_creation_input_tokens": True,
    "cache_read_input_tokens": True,
    "cache_creation": {"ephemeral_5m_input_tokens": True, "ephemeral_1h_input_tokens": True},
    "server_tool_use": {"web_search_requests": True, "web_fetch_requests": True},
}

WRITE_TIERS: tuple[tuple[str, str], ...] = (
    ("ephemeral_5m_input_tokens", "ephemeral_5m"),
    ("ephemeral_1h_input_tokens", "ephemeral_1h"),
)


def normalize_messages(usage: Mapping[str, Any] | None) -> NormalizedUsage:
    result = NormalizedUsage()
    if usage is None:
        return finish(result)
    result.input_uncached = get_int(usage, "input_tokens")
    result.output = get_int(usage, "output_tokens")

    if has_key(usage, "cache_read_input_tokens"):
        result.input_cache_read = get_int(usage, "cache_read_input_tokens")
        if result.input_cache_read is None:
            result.input_cache_read = 0
            result.note("cache_read_null_treated_as_zero")
    else:
        result.note("cache_read_field_absent")

    write_total = None
    if has_key(usage, "cache_creation_input_tokens"):
        write_total = get_int(usage, "cache_creation_input_tokens")
        if write_total is None:
            write_total = 0
            result.note("cache_write_null_treated_as_zero")
    else:
        result.note("cache_write_field_absent")

    breakdown: dict[str, int] | None = None
    if has_key(usage, "cache_creation"):
        breakdown = {}
        for source_key, tier in WRITE_TIERS:
            value = get_int(usage, "cache_creation", source_key)
            if value is not None:
                breakdown[tier] = value
        if not breakdown:
            breakdown = None
            result.note("cache_creation_breakdown_empty")

    if breakdown is not None:
        tier_sum = sum(breakdown.values())
        if write_total is not None and tier_sum != write_total:
            result.completeness = UsageCompleteness.invalid
            result.note("cache_write_breakdown_mismatch")
            result.input_cache_write = write_total
            result.cache_write_breakdown = breakdown
            return result
        result.input_cache_write = tier_sum
        result.cache_write_breakdown = breakdown
    elif write_total is not None:
        result.input_cache_write = write_total
        if write_total > 0:
            result.note("cache_write_tier_unknown")

    parts = (result.input_uncached, result.input_cache_read, result.input_cache_write)
    if all(v is not None for v in parts):
        result.input_total = sum(v for v in parts if v is not None)
    return finish(result)
