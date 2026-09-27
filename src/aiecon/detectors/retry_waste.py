"""Discarded attempts and failed runs (PLAN.md §9.2).

* ``retry_waste.discarded_attempt`` - a call in a node lineage whose result the application
  marked ``discarded`` while a later attempt exists. Its known cost is money spent on an
  unused answer. Savings are only modeled when the label says the attempt was removable
  in a scenario; otherwise the cost is reported as "to investigate".
* ``retry_waste.failed_run`` - a terminal ``failed``/``abandoned`` run. All of its known call
  cost is reported; savings stay ``null`` because failure does not make the spend avoidable.
"""

from __future__ import annotations

from decimal import Decimal

from aiecon.detectors.common import DETECTOR_VERSION, DetectorInputs, event_ids, finding_id
from aiecon.spec.call import ModelCall
from aiecon.spec.envelope import Disposition, OutcomeStatus
from aiecon.spec.finding import ConfidenceClass, DetectorName, EvidenceLevel, Finding


def _later_attempt_exists(call: ModelCall, siblings: list[ModelCall]) -> bool:
    for other in siblings:
        if other.call_id == call.call_id:
            continue
        if other.retry_of_call_id == call.call_id or other.fallback_of_call_id == call.call_id:
            return True
        if (
            other.attempt_index is not None
            and call.attempt_index is not None
            and other.attempt_index > call.attempt_index
        ):
            return True
    return False


def detect_discarded_attempts(inputs: DetectorInputs) -> list[Finding]:
    findings: list[Finding] = []
    by_node = inputs.calls_by_node_run()
    for node_run_id, siblings in sorted(by_node.items()):
        for call in sorted(siblings, key=lambda c: (c.attempt_index or 0, c.call_id)):
            disposition = inputs.disposition(call)
            if disposition is None or disposition.disposition is not Disposition.discarded:
                continue
            # A discarded fallback that was itself superseded is reported here as well as by
            # fallback_waste; the summary de-duplicates by line item (PLAN.md 9.3).
            if not _later_attempt_exists(call, siblings):
                continue
            cost = inputs.cost(call)
            known = cost.known_usd if cost.line_item_ids else None
            removable = disposition.removal_safe_in_scenario is True and cost.complete
            if removable:
                savings: Decimal | None = cost.known_usd
                level = EvidenceLevel.labeled
                action = (
                    "The application labeled this attempt as safely removable; test the flow "
                    "without it and confirm the retry rate does not rise."
                )
            else:
                savings = None
                level = EvidenceLevel.observed if cost.complete else EvidenceLevel.insufficient
                action = (
                    "Cost of an attempt whose result was discarded; investigate why it was "
                    "superseded before assuming it is avoidable."
                )
            caveats = ["a retry may have been necessary; avoidability is a scenario, not a fact"]
            if not cost.complete:
                caveats.append("usage or price missing: the amount is a known-cost lower bound")
            if call.status.value != "success":
                caveats.append(
                    f"attempt ended with status {call.status.value}; provider billing is unknown"
                )
            findings.append(
                Finding(
                    finding_id=finding_id("discarded", inputs.dataset_id, call.call_id),
                    detector=DetectorName.discarded_attempt,
                    detector_version=DETECTOR_VERSION,
                    dataset_id=inputs.dataset_id,
                    data_kind=call.data_kind,
                    scope_id=call.scope_id,
                    evidence_event_ids=event_ids(call),
                    call_ids=[call.call_id],
                    line_item_ids=cost.line_item_ids,
                    affected_run_ids=[call.workflow_run_id] if call.workflow_run_id else [],
                    observed_cost_usd=known,
                    observed_cost_complete=cost.complete,
                    unknown_cost_call_count=0 if cost.complete else 1,
                    modeled_savings_usd=savings,
                    confidence_class=ConfidenceClass.high if removable else ConfidenceClass.medium,
                    evidence_level=level,
                    proposed_action=action,
                    assumptions=(
                        ["removing the attempt does not change the later attempt's cost"]
                        if removable
                        else []
                    ),
                    caveats=caveats,
                    summary=(
                        f"Attempt {call.attempt_index or '?'} of node run {node_run_id} was "
                        f"discarded ({disposition.reason or 'no reason'}) and superseded by a "
                        "later attempt."
                    ),
                )
            )
    return findings


def detect_failed_runs(inputs: DetectorInputs) -> list[Finding]:
    findings: list[Finding] = []
    by_run = inputs.calls_by_run()
    for run_id, outcome in sorted(inputs.outcomes.items()):
        if outcome.status not in (OutcomeStatus.failed, OutcomeStatus.abandoned):
            continue
        calls = sorted(by_run.get(run_id, []), key=lambda c: (c.started_at_ms or 0, c.call_id))
        if not calls:
            continue
        costs = [inputs.cost(c) for c in calls]
        known = sum((c.known_usd for c in costs), start=Decimal(0))
        complete = all(c.complete for c in costs)
        unknown = sum(1 for c in costs if not c.complete)
        findings.append(
            Finding(
                finding_id=finding_id("failedrun", inputs.dataset_id, run_id),
                detector=DetectorName.failed_run,
                detector_version=DETECTOR_VERSION,
                dataset_id=inputs.dataset_id,
                data_kind=calls[0].data_kind,
                scope_id=calls[0].scope_id,
                evidence_event_ids=[*outcome.source_event_ids, *event_ids(*calls)],
                call_ids=[c.call_id for c in calls],
                line_item_ids=[li for c in costs for li in c.line_item_ids],
                affected_run_ids=[run_id],
                observed_cost_usd=known,
                observed_cost_complete=complete,
                unknown_cost_call_count=unknown,
                modeled_savings_usd=None,
                confidence_class=ConfidenceClass.high,
                evidence_level=EvidenceLevel.labeled,
                proposed_action=(
                    "Review why the run ended as "
                    f"{outcome.status.value}; consider failing earlier or cheaper before the "
                    "expensive nodes run."
                ),
                assumptions=[],
                caveats=[
                    "a failed run's cost is not automatically avoidable",
                    *(["some calls have unknown cost: amount is a lower bound"] if unknown else []),
                ],
                summary=(
                    f"Run {run_id} ended {outcome.status.value} after {len(calls)} calls with a "
                    f"known cost of ${known:f}."
                ),
            )
        )
    return findings
