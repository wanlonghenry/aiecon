"""V04, V05, V08: fixture-driven usage normalization and streaming final-usage rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiecon.adapters import normalize_usage
from aiecon.adapters.anthropic import merge_stream_usage
from aiecon.adapters.openai import final_stream_usage
from aiecon.spec import UsageCompleteness, UsageFormat

FIXTURES = sorted((Path(__file__).parent.parent / "fixtures" / "usage").glob("*.json"))


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
def test_usage_fixture_normalizes_to_expected(path: Path) -> None:
    fixture = json.loads(path.read_text("utf-8"))
    result = normalize_usage(UsageFormat(fixture["usage_format"]), fixture["usage"])
    expected = fixture["expected"]
    assert result.input_total == expected["input_total"]
    assert result.input_uncached == expected["input_uncached"]
    assert result.input_cache_read == expected["input_cache_read"]
    assert result.input_cache_write == expected["input_cache_write"]
    assert result.output == expected["output"]
    assert result.cache_write_breakdown == expected["cache_write_breakdown"]
    assert result.completeness is UsageCompleteness(expected["completeness"])
    for note in expected["notes"]:
        assert note in result.notes


def test_both_provider_shapes_land_on_the_same_split() -> None:
    openai = normalize_usage(
        UsageFormat.openai_responses,
        {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 100},
            "output_tokens": 100,
        },
    )
    anthropic = normalize_usage(
        UsageFormat.anthropic_messages,
        {
            "input_tokens": 300,
            "cache_read_input_tokens": 600,
            "cache_creation_input_tokens": 100,
            "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 0},
            "output_tokens": 100,
        },
    )
    for result in (openai, anthropic):
        assert (result.input_uncached, result.input_cache_read, result.input_cache_write) == (
            300,
            600,
            100,
        )
        assert result.output == 100 and result.input_total == 1000


def test_anthropic_stream_uses_cumulative_final_delta_not_a_sum() -> None:
    message_start = {
        "input_tokens": 2679,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 3,
    }
    deltas = [{"output_tokens": 15}, {"output_tokens": 60}, {"output_tokens": 89}]
    merged = merge_stream_usage(message_start, deltas[-1])
    result = normalize_usage(UsageFormat.anthropic_messages, merged)
    assert result.output == 89  # not 3 + 15 + 60 + 89
    assert result.input_total == 2679
    assert result.completeness is UsageCompleteness.complete
    # a delta carrying cumulative input fields overrides message_start
    grown = merge_stream_usage(message_start, {"input_tokens": 10682, "output_tokens": 510})
    assert normalize_usage(UsageFormat.anthropic_messages, grown).input_uncached == 10682


def test_anthropic_stream_interrupted_before_any_delta_is_partial() -> None:
    merged = merge_stream_usage({"input_tokens": 2679, "output_tokens": 1}, None)
    result = normalize_usage(UsageFormat.anthropic_messages, merged)
    assert result.completeness is UsageCompleteness.partial


def test_openai_stream_final_chunk_carries_usage_and_interruption_is_missing() -> None:
    chunks = [
        {"choices": [{"delta": {"content": "a"}}], "usage": None},
        {"choices": [{"delta": {"content": "b"}}], "usage": None},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            },
        },
    ]
    final = final_stream_usage(chunks)
    result = normalize_usage(UsageFormat.openai_chat_completions, final)
    assert result.output == 20 and result.completeness is UsageCompleteness.complete
    interrupted = final_stream_usage(chunks[:2])
    assert interrupted is None
    missing = normalize_usage(UsageFormat.openai_chat_completions, interrupted)
    assert missing.completeness is UsageCompleteness.missing
