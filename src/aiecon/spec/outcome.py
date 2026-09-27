"""Outcome: business terminal state of a workflow run (PLAN.md §4.4)."""

from __future__ import annotations

from pydantic import Field

from aiecon.spec.common import DataKind, IdStr, PosInt, SpecModel, UtcMs
from aiecon.spec.envelope import CallDisposition, OutcomeSource, OutcomeStatus


class Outcome(SpecModel):
    dataset_id: IdStr
    workflow_run_id: IdStr
    workflow_id: IdStr | None = None
    data_kind: DataKind
    revision: PosInt = 1
    terminal_at_ms: UtcMs | None = None
    status: OutcomeStatus
    success: bool | None = None
    outcome_source: OutcomeSource
    call_dispositions: list[CallDisposition] = Field(default_factory=list)
    source_event_ids: list[IdStr] = Field(default_factory=list)
    source_content_hashes: list[str] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OutcomeStatus.succeeded,
            OutcomeStatus.failed,
            OutcomeStatus.abandoned,
        )

    def disposition_for(self, call_id: str) -> CallDisposition | None:
        for item in self.call_dispositions:
            if item.call_id == call_id:
                return item
        return None
