"""Unused or redundant fallbacks (PLAN.md §9.2).

Only calls with an explicit ``fallback_of_call_id`` are considered. A fallback whose result
was used is never a finding. A fallback labeled ``discarded`` is reported with its known
cost; savings are modeled only when the application marked it removable in a scenario. An
``unknown`` disposition yields a finding without savings that asks for labels.
"""

from __future__ import annotations

from decimal import Decimal

from aiecon.detectors.common import DETECTOR_VERSION, DetectorInputs, event_ids, finding_id
from aiecon.spec.envelope import Disposition
from aiecon.spec.finding import ConfidenceClass, DetectorName, EvidenceLevel, Finding


def detect_unused_fallbacks(inputs: DetectorInputs) -> list[Finding]:
    findings: list[Finding] = []
    for call in sorted(inputs.calls, key=lambda c: (c.started_at_ms or 0, c.call_id)):
        if call.fallback_of_call_id is None:
            continue
        disposition = inputs.disposition(call)
        state = disposition.disposition if disposition else Disposition.unknown
        if state is Disposition.used:
            continue
        cost = inputs.cost(call)
        known = cost.known_usd if cost.line_item_ids else None
        removable = (
            state is Disposition.discarded
            and disposition is not None
            and disposition.removal_safe_in_scenario is True
            and cost.complete
        )
        if removable:
            savings: Decimal | None = cost.known_usd
            level = EvidenceLevel.labeled
            confidence = ConfidenceClass.high
            action = (
                "The primary attempt succeeded and this fallback was labeled redundant; try "
                "gating the fallback on the primary result instead of firing it eagerly."
            )
        elif state is Disposition.discarded:
            savings = None
            level = EvidenceLevel.observed if cost.complete else EvidenceLevel.insufficient
            confidence = ConfidenceClass.medium
            action = (
                "Fallback result was discarded; check whether the primary path could have "
                "been trusted."
            )
        else:
            savings = None
            level = EvidenceLevel.insufficient
            confidence = ConfidenceClass.low
            action = "Label whether this fallback's result was used before treating it as waste."
        caveats = ["a fallback that raised success rate has value; removal is a scenario"]
        if not cost.complete:
            caveats.append("usage or price missing: the amount is a known-cost lower bound")
        findings.append(
            Finding(
                finding_id=finding_id("fallback", inputs.dataset_id, call.call_id),
                detector=DetectorName.unused_fallback,
                detector_version=DETECTOR_VERSION,
                dataset_id=inputs.dataset_id,
                data_kind=call.data_kind,
                scope_id=call.scope_id,
                evidence_event_ids=event_ids(call),
                call_ids=[call.call_id, call.fallback_of_call_id],
                line_item_ids=cost.line_item_ids,
                affected_run_ids=[call.workflow_run_id] if call.workflow_run_id else [],
                observed_cost_usd=known,
                observed_cost_complete=cost.complete,
                unknown_cost_call_count=0 if cost.complete else 1,
                modeled_savings_usd=savings,
                confidence_class=confidence,
                evidence_level=level,
                proposed_action=action,
                assumptions=(
                    ["the primary result is acceptable without the fallback"] if removable else []
                ),
                caveats=caveats,
                summary=(
                    f"Fallback call {call.call_id} ({call.provider.value}) after "
                    f"{call.fallback_of_call_id} ended with disposition {state.value}."
                ),
            )
        )
    return findings
