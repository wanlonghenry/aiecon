"""V08–V10: both provider usage shapes normalize to one set of mutually exclusive metrics."""

from __future__ import annotations

from aiecon.adapters import normalize_usage
from aiecon.spec import UsageCompleteness, UsageFormat

# PLAN.md section 13.1: OpenAI-format total=1000/read=600/write=100 and Anthropic-format
# uncached=300/read=600/write=100 must land on the same (300, 600, 100) split.


def test_openai_responses_format_with_cache_write_counter() -> None:
    usage = {
        "input_tokens": 1000,
        "input_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 100},
        "output_tokens": 100,
        "output_tokens_details": {"reasoning_tokens": 40},
    }
    result = normalize_usage(UsageFormat.openai_responses, usage)
    assert (result.input_uncached, result.input_cache_read, result.input_cache_write) == (
        300,
        600,
        100,
    )
    assert result.input_total == 1000
    assert result.output == 100  # reasoning is a subset of output, never added (V10)
    assert result.completeness is UsageCompleteness.complete


def test_openai_chat_format_without_write_counter_is_zero_by_contract() -> None:
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "total_tokens": 1100,
        "prompt_tokens_details": {"cached_tokens": 600},
        "completion_tokens_details": {"reasoning_tokens": 40},
    }
    result = normalize_usage(UsageFormat.openai_chat_completions, usage)
    assert (result.input_uncached, result.input_cache_read, result.input_cache_write) == (
        400,
        600,
        0,
    )
    assert "cache_write_field_absent" in result.notes
    assert result.completeness is UsageCompleteness.complete


def test_openai_format_missing_cache_details_is_partial_not_zero() -> None:
    result = normalize_usage(
        UsageFormat.openai_responses, {"input_tokens": 1000, "output_tokens": 100}
    )
    assert result.input_total == 1000
    assert result.input_cache_read is None
    assert result.input_uncached is None
    assert result.completeness is UsageCompleteness.partial


def test_anthropic_format_adds_uncached_and_cache_parts() -> None:
    usage = {
        "input_tokens": 300,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 600,
        "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 0},
        "output_tokens": 100,
    }
    result = normalize_usage(UsageFormat.anthropic_messages, usage)
    assert (result.input_uncached, result.input_cache_read, result.input_cache_write) == (
        300,
        600,
        100,
    )
    assert result.input_total == 1000
    assert result.cache_write_breakdown == {"ephemeral_5m": 100, "ephemeral_1h": 0}
    assert result.completeness is UsageCompleteness.complete


def test_anthropic_write_total_and_tiers_price_once(V09: None = None) -> None:
    usage = {
        "input_tokens": 300,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 600,
        "cache_creation": {"ephemeral_5m_input_tokens": 60, "ephemeral_1h_input_tokens": 40},
        "output_tokens": 100,
    }
    result = normalize_usage(UsageFormat.anthropic_messages, usage)
    assert result.input_cache_write == 100
    assert sum(result.cache_write_breakdown.values()) == 100
    mismatch = dict(usage, cache_creation_input_tokens=150)
    bad = normalize_usage(UsageFormat.anthropic_messages, mismatch)
    assert bad.completeness is UsageCompleteness.invalid
    assert "cache_write_breakdown_mismatch" in bad.notes


def test_anthropic_write_without_tier_is_flagged() -> None:
    usage = {
        "input_tokens": 300,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 600,
        "output_tokens": 100,
    }
    result = normalize_usage(UsageFormat.anthropic_messages, usage)
    assert result.input_cache_write == 100 and result.cache_write_breakdown is None
    assert "cache_write_tier_unknown" in result.notes


def test_litellm_transformed_anthropic_usage_is_not_double_counted() -> None:
    # LiteLLM folds cache tokens into prompt_tokens; adding the native way would give 2000
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "total_tokens": 1100,
        "prompt_tokens_details": {"cached_tokens": 600},
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 600,
        "cache_creation_token_details": {
            "ephemeral_5m_input_tokens": 100,
            "ephemeral_1h_input_tokens": 0,
        },
    }
    result = normalize_usage(UsageFormat.litellm_standard, usage)
    assert result.input_total == 1000
    assert (result.input_uncached, result.input_cache_read, result.input_cache_write) == (
        300,
        600,
        100,
    )
    assert result.completeness is UsageCompleteness.complete


def test_litellm_nested_cache_breakdown_is_read_from_prompt_tokens_details() -> None:
    # LiteLLM 1.102.1 live layout: the 5m/1h split sits under prompt_tokens_details
    usage = {
        "prompt_tokens": 30829,
        "completion_tokens": 120,
        "prompt_tokens_details": {
            "cached_tokens": 0,
            "cache_creation_tokens": 30812,
            "cache_creation_token_details": {
                "ephemeral_5m_input_tokens": 30812,
                "ephemeral_1h_input_tokens": 0,
            },
        },
        "cache_creation_input_tokens": 30812,
        "cache_read_input_tokens": 0,
    }
    result = normalize_usage(UsageFormat.litellm_standard, usage)
    assert (result.input_uncached, result.input_cache_read, result.input_cache_write) == (
        17,
        0,
        30812,
    )
    assert result.cache_write_breakdown == {"ephemeral_5m": 30812, "ephemeral_1h": 0}
    assert result.completeness is UsageCompleteness.complete
    # without any breakdown the write stays unpriceable, as before
    del usage["prompt_tokens_details"]["cache_creation_token_details"]
    bare = normalize_usage(UsageFormat.litellm_standard, usage)
    assert "cache_write_tier_unknown" in bare.notes


def test_missing_usage_is_missing_not_zero() -> None:
    result = normalize_usage(UsageFormat.anthropic_messages, None)
    assert result.completeness is UsageCompleteness.missing
    assert result.input_total is None and result.output is None
    unknown = normalize_usage(UsageFormat.unknown, {"prompt_tokens": 5})
    assert unknown.completeness is UsageCompleteness.missing
    assert "usage_format_unknown" in unknown.notes


def test_cached_exceeding_total_is_invalid() -> None:
    result = normalize_usage(
        UsageFormat.openai_responses,
        {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 600}, "output_tokens": 1},
    )
    assert result.completeness is UsageCompleteness.invalid
