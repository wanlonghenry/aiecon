"""Repeated context findings built on Context Economics groups (PLAN.md §8, §9.2)."""

from __future__ import annotations

from aiecon.context_econ import ContextEconResult
from aiecon.detectors.common import DETECTOR_VERSION, DetectorInputs, finding_id
from aiecon.spec.finding import (
    ConfidenceClass,
    ContextEconGroup,
    ContextRecommendation,
    DetectorName,
    EvidenceLevel,
    Finding,
)
from aiecon.spec.pricing import INPUT_RESOURCES


def _input_line_items(inputs: DetectorInputs, group: ContextEconGroup) -> list[str]:
    ids: list[str] = []
    for call_id in group.call_ids:
        for li in inputs.items_by_call.get(call_id, []):
            if li.resource in INPUT_RESOURCES:
                ids.append(li.line_item_id)
    return ids


def detect_repeated_context(inputs: DetectorInputs, context: ContextEconResult) -> list[Finding]:
    findings: list[Finding] = []
    calls_by_id = {c.call_id: c for c in inputs.calls}
    for group in context.groups:
        if group.recommendation is not ContextRecommendation.beneficial:
            continue
        calls = [calls_by_id[c] for c in group.call_ids if c in calls_by_id]
        runs = sorted({c.workflow_run_id for c in calls if c.workflow_run_id})
        observed = sum(
            (
                li.line_cost
                for c in calls
                for li in inputs.items_by_call.get(c.call_id, [])
                if li.resource in INPUT_RESOURCES and li.line_cost is not None
            ),
            start=0,
        )
        findings.append(
            Finding(
                finding_id=finding_id("context", inputs.dataset_id, group.group_id),
                detector=DetectorName.repeated_context,
                detector_version=DETECTOR_VERSION,
                dataset_id=inputs.dataset_id,
                data_kind=group.data_kind,
                scope_id=group.scope_id,
                evidence_event_ids=[e for c in calls for e in c.source_event_ids],
                call_ids=list(group.call_ids),
                line_item_ids=_input_line_items(inputs, group),
                affected_run_ids=runs,
                observed_cost_usd=observed if calls else None,
                observed_cost_complete=all(inputs.cost(c).complete for c in calls)
                if calls
                else None,
                unknown_cost_call_count=sum(1 for c in calls if not inputs.cost(c).complete),
                modeled_savings_usd=group.modeled_savings_usd,
                confidence_class=group.confidence,
                evidence_level=EvidenceLevel.modeled,
                proposed_action=(
                    f"Enable cache policy {group.scenario_policy} for this prefix on "
                    f"{group.model_resolved} and verify provider-reported cache reads afterwards."
                ),
                assumptions=list(group.assumptions),
                caveats=list(group.caveats),
                summary=(
                    f"This {group.prefix_tokens}-token prefix appeared in {group.calls} calls "
                    "(observed). Provider-reported cache usage: "
                    f"{group.observed_cache_read_tokens} tokens read / "
                    f"{group.observed_cache_write_tokens} written (observed). "
                    f"Modeled cache scenario under the stated assumptions: "
                    f"${group.modeled_savings_usd:f} savings, break-even at "
                    f"{group.break_even_reuses} reuses (modeled)."
                ),
                scenario={
                    "policy": group.scenario_policy,
                    "segments": len(group.segments),
                    "no_cache_cost_usd": None
                    if group.modeled_no_cache_cost_usd is None
                    else format(group.modeled_no_cache_cost_usd, "f"),
                    "cache_cost_usd": None
                    if group.modeled_cache_cost_usd is None
                    else format(group.modeled_cache_cost_usd, "f"),
                },
            )
        )
    _ = ConfidenceClass  # keep import for type parity with other detectors
    return findings
