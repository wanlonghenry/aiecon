"""Context Economics v1 (PLAN.md §8): prefix reuse, observed cache usage, modeled scenarios.

Three facts are kept apart for every group of calls that shared a fingerprinted prefix:

* fingerprint repetition - the application sent the same visible prefix N times
* provider cache usage - what the provider actually reported as read / written
* optimization scenario - what the same sequence would have cost under a cache policy the
  catalog verifies for the model, using the §8.3 formula per cold-start segment

Groups are keyed by scope, provider, resolved model, cache policy, fingerprint key and
fingerprint; reuse is never inferred across scopes or keys, and never from equal token
counts. Calls without prefix evidence are only counted.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal

from aiecon.pricing.catalog import CatalogIndex
from aiecon.spec.call import ModelCall, UsageCompleteness
from aiecon.spec.finding import (
    CacheSegment,
    ConfidenceClass,
    ContextEconGroup,
    ContextRecommendation,
    EvidenceLevel,
)
from aiecon.spec.pricing import Resource

MILLION = Decimal(1_000_000)
TWELVE = Decimal("1.000000000000")

# TTL semantics per cache policy code (documented assumptions; see docs/provider-assumptions.md)
POLICY_TTL_MS: dict[str, int] = {
    "ephemeral_5m": 5 * 60_000,
    "ephemeral_1h": 60 * 60_000,
    "provider_auto": 5 * 60_000,  # conservative: OpenAI in_memory retention is 5-10 minutes
}
POLICY_WRITE_RESOURCE: dict[str, Resource | None] = {
    "ephemeral_5m": Resource.input_cache_write_5m,
    "ephemeral_1h": Resource.input_cache_write_1h,
    "provider_auto": None,  # no separate write charge: the first pass is billed as uncached
}
ALREADY_CACHED_COVERAGE = Decimal("0.9")


@dataclass
class Scenario:
    policy: str
    segments: list[CacheSegment]
    no_cache_cost: Decimal
    cache_cost: Decimal

    @property
    def savings(self) -> Decimal:
        return self.no_cache_cost - self.cache_cost


@dataclass
class ContextEconResult:
    groups: list[ContextEconGroup]
    calls_with_prefix_evidence: int
    calls_without_prefix_evidence: int
    scopes_without_fingerprint_key: int = 0
    notes: list[str] = field(default_factory=list)


def _q(value: Decimal) -> Decimal:
    return value.quantize(TWELVE, rounding=ROUND_HALF_EVEN)


def break_even_reuses(pu: Decimal, pw: Decimal, pr: Decimal) -> int | None:
    """Smallest N with N*T*Pu > T*Pw + (N-1)*T*Pr, i.e. N > (Pw - Pr) / (Pu - Pr)."""

    if pu <= pr:
        return None
    ratio = (pw - pr) / (pu - pr)
    n = int(ratio) + 1
    return max(n, 1)


def segments_for(calls: list[ModelCall], ttl_ms: int) -> list[CacheSegment]:
    """Sequential cold-start segments: a new write whenever the gap exceeds the TTL.

    The lifetime is measured from the start of the request that wrote or read the entry,
    so gaps are start-to-start.
    """

    ordered = sorted(calls, key=lambda c: (c.started_at_ms or 0, c.call_id))
    segments: list[CacheSegment] = []
    current: list[ModelCall] = []
    last_start: int | None = None
    for call in ordered:
        start = call.started_at_ms or 0
        if current and last_start is not None and start - last_start > ttl_ms:
            segments.append(_segment(current))
            current = []
        current.append(call)
        last_start = start
    if current:
        segments.append(_segment(current))
    return segments


def _segment(calls: list[ModelCall]) -> CacheSegment:
    return CacheSegment(
        first_call_id=calls[0].call_id,
        call_count=len(calls),
        started_at_ms=calls[0].started_at_ms or 0,
        ended_at_ms=max(c.ended_at_ms or c.started_at_ms or 0 for c in calls),
    )


def scenario_cost(
    n: int, prefix_tokens: int, segments: int, pu: Decimal, pw: Decimal, pr: Decimal
) -> tuple[Decimal, Decimal]:
    t = Decimal(prefix_tokens)
    no_cache = Decimal(n) * t * pu
    cache = Decimal(segments) * t * pw + Decimal(n - segments) * t * pr
    return no_cache, cache


def _unit_price(
    index: CatalogIndex, call: ModelCall, model_id: str, resource: Resource
) -> Decimal | None:
    ts = call.price_selection_ts_ms
    if ts is None:
        return None
    record = index.price_at(call.provider, model_id, resource, ts)
    if record is None:
        return None
    return record.unit_price / Decimal(record.unit_quantity)


def analyze(calls: list[ModelCall], index: CatalogIndex, *, dataset_id: str) -> ContextEconResult:
    with_prefix = [c for c in calls if c.prefix_fingerprint]
    without = sum(1 for c in calls if not c.prefix_fingerprint)
    groups: dict[tuple, list[ModelCall]] = defaultdict(list)
    for call in with_prefix:
        key = (
            call.scope_id,
            call.provider.value,
            call.model_resolved or call.model_requested,
            call.cache_policy or "unknown",
            call.fingerprint_key_id or "-",
            call.prefix_fingerprint,
        )
        groups[key].append(call)

    result_groups: list[ContextEconGroup] = []
    for index_no, (key, members) in enumerate(
        sorted(groups.items(), key=lambda kv: kv[0]), start=1
    ):
        scope_id, provider, model, policy, key_id, fingerprint = key
        members.sort(key=lambda c: (c.started_at_ms or 0, c.call_id))
        usable = [c for c in members if c.usage_completeness is UsageCompleteness.complete]
        data_kind = members[0].data_kind
        prefix_values = {c.prefix_tokens for c in members if c.prefix_tokens is not None}
        prefix_tokens = max(prefix_values) if prefix_values else None
        method = next(
            (c.prefix_token_count_method for c in members if c.prefix_token_count_method), None
        )
        observed_read = sum(c.input_cache_read_tokens or 0 for c in usable)
        observed_write = sum(c.input_cache_write_tokens or 0 for c in usable)
        starts = sorted(c.started_at_ms or 0 for c in members)
        intervals = [b - a for a, b in zip(starts, starts[1:], strict=False)]
        assumptions: list[str] = []
        caveats: list[str] = []
        common = {
            "group_id": f"ctx_{index_no:04d}_{fingerprint[:12]}",
            "dataset_id": dataset_id,
            "data_kind": data_kind,
            "scope_id": scope_id,
            "provider": members[0].provider,
            "model_resolved": model,
            "cache_policy": None if policy == "unknown" else policy,
            "fingerprint_key_id": None if key_id == "-" else key_id,
            "prefix_fingerprint": fingerprint,
            "calls": len(members),
            "call_ids": [c.call_id for c in members],
            "prefix_tokens": prefix_tokens,
            "prefix_token_count_method": method,
            "first_seen_ms": starts[0],
            "last_seen_ms": starts[-1],
            "reuse_intervals_ms": intervals,
            "observed_cache_read_tokens": observed_read if usable else None,
            "observed_cache_write_tokens": observed_write if usable else None,
        }
        model_id, _basis = index.resolve_model(
            members[0].provider, model, members[0].model_requested
        )
        contract = index.contract(members[0].provider, model_id) if model_id else None

        def insufficient(
            reason: str,
            _common: dict = common,
            _assumptions: list[str] = assumptions,
            _caveats: list[str] = caveats,
        ) -> ContextEconGroup:
            return ContextEconGroup(
                **_common,
                recommendation=ContextRecommendation.insufficient_evidence,
                evidence_level=EvidenceLevel.insufficient,
                confidence=ConfidenceClass.low,
                assumptions=list(_assumptions),
                caveats=[*_caveats, reason],
            )

        if prefix_tokens is None:
            result_groups.append(insufficient("prefix token count unavailable"))
            continue
        if len(prefix_values) > 1:
            caveats.append("prefix token counts differ across calls; the largest is used")
        if not usable:
            result_groups.append(insufficient("no call in the group has complete usage"))
            continue
        if model_id is None or contract is None:
            result_groups.append(
                insufficient("model has no verified cache contract in the catalog")
            )
            continue
        sample = usable[0]
        pu = _unit_price(index, sample, model_id, Resource.input_uncached)
        pr = _unit_price(index, sample, model_id, Resource.input_cache_read)
        if pu is None or pr is None:
            result_groups.append(insufficient("uncached or cache-read price missing"))
            continue
        if method and method.startswith("estimated"):
            caveats.append("prefix tokens are estimated, not provider-counted (evidence class D)")

        n = len(usable)
        candidate_repeat = max(0, (n - 1) * prefix_tokens - observed_read)
        if n > 1 and Decimal(observed_read) >= ALREADY_CACHED_COVERAGE * Decimal(
            (n - 1) * prefix_tokens
        ):
            result_groups.append(
                ContextEconGroup(
                    **common,
                    candidate_repeated_prefix_tokens=candidate_repeat,
                    recommendation=ContextRecommendation.already_cached,
                    evidence_level=EvidenceLevel.observed,
                    confidence=ConfidenceClass.high,
                    supported_ttl_candidates=list(contract.supported_cache_policies),
                    assumptions=["provider-reported cache reads cover the repeated prefix"],
                    caveats=[
                        *caveats,
                        "tokens already served from cache are not counted as potential savings",
                    ],
                )
            )
            continue

        scenarios: list[Scenario] = []
        for candidate in contract.supported_cache_policies:
            ttl = POLICY_TTL_MS.get(candidate)
            if ttl is None:
                continue
            write_resource = POLICY_WRITE_RESOURCE.get(candidate)
            if write_resource is None:
                pw = pu  # no write charge: the first pass is ordinary input
            else:
                pw_price = _unit_price(index, sample, model_id, write_resource)
                if pw_price is None:
                    continue
                pw = pw_price
            segs = segments_for(usable, ttl)
            no_cache, cache = scenario_cost(n, prefix_tokens, len(segs), pu, pw, pr)
            scenarios.append(Scenario(candidate, segs, no_cache, cache))
        if not scenarios:
            result_groups.append(insufficient("no supported cache policy has verified prices"))
            continue

        best = max(scenarios, key=lambda s: s.savings)
        best_write = POLICY_WRITE_RESOURCE.get(best.policy)
        pw_best = (
            pu if best_write is None else (_unit_price(index, sample, model_id, best_write) or pu)
        )
        assumptions.extend(
            [
                f"scenario policy {best.policy}: TTL {POLICY_TTL_MS[best.policy] // 60_000} min, "
                "sequential calls, no eviction, one write per cold-start segment",
                "the whole fingerprinted prefix is cacheable and repeated verbatim",
                "prices from the catalog used by the active pricing run",
            ]
        )
        caveats.append(
            "scenario values are modeled, not observed; provider cache behaviour may differ"
        )
        if observed_read:
            caveats.append("existing cache reads are excluded from the candidate repeated prefix")
        recommendation = (
            ContextRecommendation.beneficial
            if best.savings > 0
            else ContextRecommendation.not_beneficial
        )
        result_groups.append(
            ContextEconGroup(
                **common,
                candidate_repeated_prefix_tokens=candidate_repeat,
                scenario_policy=best.policy,
                segments=best.segments,
                modeled_no_cache_cost_usd=_q(best.no_cache_cost),
                modeled_cache_cost_usd=_q(best.cache_cost),
                modeled_savings_usd=_q(best.savings),
                break_even_reuses=break_even_reuses(pu, pw_best, pr),
                supported_ttl_candidates=[s.policy for s in scenarios],
                recommendation=recommendation,
                evidence_level=EvidenceLevel.modeled,
                confidence=ConfidenceClass.medium if n >= 3 else ConfidenceClass.low,
                assumptions=assumptions,
                caveats=caveats,
            )
        )

    return ContextEconResult(
        groups=result_groups,
        calls_with_prefix_evidence=len(with_prefix),
        calls_without_prefix_evidence=without,
    )
