"""Shared result type for usage adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aiecon.spec.call import UsageCompleteness


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
