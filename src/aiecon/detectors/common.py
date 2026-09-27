"""Shared inputs and cost helpers for detectors."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from aiecon.spec.call import ModelCall, UsageCompleteness
from aiecon.spec.envelope import CallDisposition
from aiecon.spec.outcome import Outcome
from aiecon.spec.pricing import CostLineItem, LineItemStatus
from aiecon.storage import Storage

DETECTOR_VERSION = "0.1.0"


@dataclass
class CallCost:
    known_usd: Decimal
    complete: bool
    line_item_ids: list[str]


@dataclass
class DetectorInputs:
    dataset_id: str
    calls: list[ModelCall]
    outcomes: dict[str, Outcome]
    items_by_call: dict[str, list[CostLineItem]] = field(default_factory=dict)

    def cost(self, call: ModelCall) -> CallCost:
        items = self.items_by_call.get(call.call_id, [])
        known = sum((li.line_cost for li in items if li.line_cost is not None), start=Decimal(0))
        complete = (
            call.usage_completeness is UsageCompleteness.complete
            and bool(items)
            and all(li.status is LineItemStatus.priced for li in items)
        )
        return CallCost(known, complete, [li.line_item_id for li in items])

    def disposition(self, call: ModelCall) -> CallDisposition | None:
        if call.workflow_run_id is None:
            return None
        outcome = self.outcomes.get(call.workflow_run_id)
        return outcome.disposition_for(call.call_id) if outcome else None

    def calls_by_run(self) -> dict[str, list[ModelCall]]:
        grouped: dict[str, list[ModelCall]] = defaultdict(list)
        for call in self.calls:
            if call.workflow_run_id is not None:
                grouped[call.workflow_run_id].append(call)
        return grouped

    def calls_by_node_run(self) -> dict[str, list[ModelCall]]:
        grouped: dict[str, list[ModelCall]] = defaultdict(list)
        for call in self.calls:
            if call.node_run_id is not None:
                grouped[call.node_run_id].append(call)
        return grouped


def load_inputs(storage: Storage, dataset_id: str) -> DetectorInputs:
    calls = storage.list_calls(dataset_id)
    outcomes = {o.workflow_run_id: o for o in storage.list_outcomes(dataset_id)}
    pricing = storage.active_pricing_run(dataset_id)
    items_by_call: dict[str, list[CostLineItem]] = defaultdict(list)
    if pricing is not None:
        for li in storage.list_line_items(pricing.pricing_run_id):
            items_by_call[li.call_id].append(li)
    return DetectorInputs(
        dataset_id=dataset_id, calls=calls, outcomes=outcomes, items_by_call=dict(items_by_call)
    )


def finding_id(detector: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"f_{detector}_{digest}"


def event_ids(*calls: ModelCall) -> list[str]:
    out: list[str] = []
    for call in calls:
        out.extend(call.source_event_ids)
    return out
