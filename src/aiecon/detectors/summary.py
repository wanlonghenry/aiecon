"""Finding aggregation without double counting (PLAN.md §9.3) and outcome economics (§9.4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal

from aiecon.detectors.common import DetectorInputs
from aiecon.spec.envelope import OutcomeStatus
from aiecon.spec.finding import EvidenceLevel, Finding

TWELVE = Decimal("1.000000000000")


def _q(value: Decimal) -> Decimal:
    return value.quantize(TWELVE, rounding=ROUND_HALF_EVEN)


@dataclass
class FindingsSummary:
    findings: list[Finding]
    unique_flagged_cost_usd: Decimal
    unique_flagged_line_items: int
    unique_flagged_calls: int
    unknown_cost_flagged_calls: int
    best_single_action_savings_usd: Decimal | None
    best_single_action_finding_id: str | None
    joint_savings: str = "not computed"
    by_detector: dict[str, int] = field(default_factory=dict)


def summarize(inputs: DetectorInputs, findings: list[Finding]) -> FindingsSummary:
    line_ids: set[str] = set()
    call_ids: set[str] = set()
    unknown_calls: set[str] = set()
    by_detector: dict[str, int] = {}
    for f in findings:
        line_ids.update(f.line_item_ids)
        call_ids.update(f.call_ids)
        by_detector[f.detector.value] = by_detector.get(f.detector.value, 0) + 1
    costs_by_line: dict[str, Decimal] = {}
    for items in inputs.items_by_call.values():
        for li in items:
            if li.line_cost is not None:
                costs_by_line[li.line_item_id] = li.line_cost
    calls_by_id = {c.call_id: c for c in inputs.calls}
    for call_id in call_ids:
        call = calls_by_id.get(call_id)
        if call is not None and not inputs.cost(call).complete:
            unknown_calls.add(call_id)
    unique_cost = sum((costs_by_line.get(i, Decimal(0)) for i in line_ids), start=Decimal(0))

    best: Finding | None = None
    for f in findings:
        if f.modeled_savings_usd is None or f.modeled_savings_usd <= 0:
            continue
        if f.evidence_level not in (EvidenceLevel.labeled, EvidenceLevel.modeled):
            continue
        if f.observed_cost_complete is False and f.evidence_level is EvidenceLevel.labeled:
            continue  # incomplete amounts cannot back a headline saving
        if best is None or f.modeled_savings_usd > (best.modeled_savings_usd or Decimal(0)):
            best = f
    return FindingsSummary(
        findings=findings,
        unique_flagged_cost_usd=_q(unique_cost),
        unique_flagged_line_items=len(line_ids),
        unique_flagged_calls=len(call_ids),
        unknown_cost_flagged_calls=len(unknown_calls),
        best_single_action_savings_usd=None
        if best is None
        else _q(best.modeled_savings_usd or Decimal(0)),
        best_single_action_finding_id=None if best is None else best.finding_id,
        by_detector=dict(sorted(by_detector.items())),
    )


@dataclass
class OutcomeEconomics:
    cohort_runs: int
    succeeded: int
    failed: int
    abandoned: int
    pending_or_unknown_runs: int
    runs_without_outcome: int
    unattributed_calls: int
    cohort_call_count: int
    cohort_known_cost_usd: Decimal
    unknown_cost_calls: int
    cost_per_successful_outcome_usd: Decimal | None
    successful_runs_only_cost_usd: Decimal
    successful_runs_only_per_success_usd: Decimal | None
    is_lower_bound: bool
    note: str


def outcome_economics(inputs: DetectorInputs) -> OutcomeEconomics:
    """Terminal-cohort cost per successful outcome (§9.4)."""

    by_run = inputs.calls_by_run()
    terminal = {
        rid: o
        for rid, o in inputs.outcomes.items()
        if o.status in (OutcomeStatus.succeeded, OutcomeStatus.failed, OutcomeStatus.abandoned)
    }
    pending = sum(
        1
        for o in inputs.outcomes.values()
        if o.status in (OutcomeStatus.pending, OutcomeStatus.unknown)
    )
    runs_without_outcome = sum(1 for rid in by_run if rid not in inputs.outcomes)
    unattributed = sum(1 for c in inputs.calls if c.workflow_run_id is None)
    total = Decimal(0)
    success_total = Decimal(0)
    unknown = 0
    call_count = 0
    for rid, outcome in terminal.items():
        for call in by_run.get(rid, []):
            cost = inputs.cost(call)
            call_count += 1
            total += cost.known_usd
            if not cost.complete:
                unknown += 1
            if outcome.status is OutcomeStatus.succeeded:
                success_total += cost.known_usd
    succeeded = sum(1 for o in terminal.values() if o.status is OutcomeStatus.succeeded)
    failed = sum(1 for o in terminal.values() if o.status is OutcomeStatus.failed)
    abandoned = sum(1 for o in terminal.values() if o.status is OutcomeStatus.abandoned)
    if succeeded == 0:
        cpso = None
        note = "no successful outcomes"
    else:
        cpso = _q(total / Decimal(succeeded))
        note = "known-cost lower bound" if unknown else "complete"
    return OutcomeEconomics(
        cohort_runs=len(terminal),
        succeeded=succeeded,
        failed=failed,
        abandoned=abandoned,
        pending_or_unknown_runs=pending,
        runs_without_outcome=runs_without_outcome,
        unattributed_calls=unattributed,
        cohort_call_count=call_count,
        cohort_known_cost_usd=_q(total),
        unknown_cost_calls=unknown,
        cost_per_successful_outcome_usd=cpso,
        successful_runs_only_cost_usd=_q(success_total),
        successful_runs_only_per_success_usd=None
        if succeeded == 0
        else _q(success_total / Decimal(succeeded)),
        is_lower_bound=unknown > 0,
        note=note,
    )
