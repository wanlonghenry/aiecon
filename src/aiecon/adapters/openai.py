"""OpenAI usage formats (PLAN.md §6.3, source S3).

Contract encoded here (see docs/provider-assumptions.md for the verified references):

* Chat Completions: ``prompt_tokens`` is the total input; ``prompt_tokens_details.cached_tokens``
  is the cached subset. ``completion_tokens`` already includes
  ``completion_tokens_details.reasoning_tokens`` so reasoning is never added again.
* Responses API: same shape with ``input_tokens`` / ``input_tokens_details.cached_tokens`` and
  ``output_tokens`` / ``output_tokens_details.reasoning_tokens``.
* Cache writes: both usage objects carry ``cache_write_tokens`` ("The unadjusted number of
  prompt tokens written to cache."). GPT-5.6 and later bill writes at 1.25x the uncached
  rate; earlier models have "No additional cache-write charge". When the counter is absent
  (older transformed payloads) the adapter records ``input_cache_write = 0`` with the note
  ``cache_write_field_absent`` so the estimator can flag a lower bound for write-billed models.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from aiecon.adapters.base import NormalizedUsage, finish, get_int, has_key
from aiecon.spec.call import UsageCompleteness

CHAT_COMPLETIONS_ALLOWLIST: Mapping[str, Any] = {
    "prompt_tokens": True,
    "completion_tokens": True,
    "total_tokens": True,
    "prompt_tokens_details": {"cached_tokens": True, "audio_tokens": True, "text_tokens": True},
    "completion_tokens_details": {
        "reasoning_tokens": True,
        "audio_tokens": True,
        "accepted_prediction_tokens": True,
        "rejected_prediction_tokens": True,
        "text_tokens": True,
    },
}

RESPONSES_ALLOWLIST: Mapping[str, Any] = {
    "input_tokens": True,
    "output_tokens": True,
    "total_tokens": True,
    "input_tokens_details": {"cached_tokens": True, "cache_write_tokens": True},
    "output_tokens_details": {"reasoning_tokens": True},
}


def _normalize(
    usage: Mapping[str, Any] | None,
    *,
    total_key: str,
    output_key: str,
    details_key: str,
) -> NormalizedUsage:
    result = NormalizedUsage()
    if usage is None:
        return finish(result)
    result.input_total = get_int(usage, total_key)
    result.output = get_int(usage, output_key)
    if has_key(usage, details_key, "cached_tokens"):
        result.input_cache_read = get_int(usage, details_key, "cached_tokens")
        if result.input_cache_read is None:
            result.note("cache_read_field_invalid")
    else:
        result.note("cache_read_field_absent")
    if has_key(usage, details_key, "cache_write_tokens"):
        result.input_cache_write = get_int(usage, details_key, "cache_write_tokens")
    else:
        # Absent counter: writes are folded into uncached. Exact for models without a
        # cache-write charge; a flagged lower bound for models that bill writes.
        result.input_cache_write = 0
        result.note("cache_write_field_absent")
    if result.input_total is not None and result.input_cache_read is not None:
        write = result.input_cache_write or 0
        uncached = result.input_total - result.input_cache_read - write
        if uncached < 0:
            result.completeness = UsageCompleteness.invalid
            result.note("cached_exceeds_total")
            return result
        result.input_uncached = uncached
    return finish(result)


def normalize_chat_completions(usage: Mapping[str, Any] | None) -> NormalizedUsage:
    return _normalize(
        usage,
        total_key="prompt_tokens",
        output_key="completion_tokens",
        details_key="prompt_tokens_details",
    )


def normalize_responses(usage: Mapping[str, Any] | None) -> NormalizedUsage:
    return _normalize(
        usage,
        total_key="input_tokens",
        output_key="output_tokens",
        details_key="input_tokens_details",
    )


def final_stream_usage(chunks: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Usage of a Chat Completions stream: only the terminal chunk carries it.

    With ``stream_options.include_usage`` every chunk has ``usage: null`` except the last one
    ("The usage field on this chunk shows the token usage statistics for the entire
    request"). An interrupted stream therefore yields ``None`` - unknown, never zero.
    """

    final: Mapping[str, Any] | None = None
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        usage = chunk.get("usage")
        if isinstance(usage, Mapping) and usage:
            final = usage
    return final
