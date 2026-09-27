"""T4.2 / T4.3 / V20 / V21 / V22 / V26: detectors, de-duplicated summary, outcome economics."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from aiecon import __version__
from aiecon.context_econ import analyze
from aiecon.detectors import (
    DetectorInputs,
    detect_discarded_attempts,
    detect_failed_runs,
    detect_repeated_context,
    detect_unused_fallbacks,
    load_inputs,
    summarize,
)
from aiecon.detectors.summary import outcome_economics
from aiecon.ingest import ingest_paths
from aiecon.pipeline import fixture_root, load_expected_metrics
from aiecon.pricing import CatalogIndex, load_synthetic_catalog
from aiecon.pricing.run import run_pricing
from aiecon.spec import (
    ApiFamily,
    CallDisposition,
    CallStatus,
    CostLineItem,
    DataKind,
    Disposition,
    EvidenceClass,
    LineItemStatus,
    ModelCall,
    Outcome,
    OutcomeSource,
    OutcomeStatus,
    Provider,
    Resource,
    UsageCompleteness,
)
from aiecon.storage import Storage, Workspace

T0 = 1_790_467_200_000


def call(cid: str, run: str, node: str, attempt: int, **kw) -> ModelCall:
    data = {
        "dataset_id": "d",
        "call_id": cid,
        "data_kind": DataKind.synthetic,
        "scope_id": "s",
        "workflow_id": "wf",
        "workflow_run_id": run,
        "node_id": node,
        "node_run_id": f"{run}_{node}",
        "attempt_index": attempt,
        "normalizer_version": "0.1.0",
        "provider": Provider.openai,
        "api_family": ApiFamily.responses,
        "model_requested": "fixture-openai-v1",
        "model_resolved": "fixture-openai-v1",
        "status": CallStatus.success,
        "started_at_ms": T0 + attempt * 1000,
        "ended_at_ms": T0 + attempt * 1000 + 500,
        "input_total_tokens": 100,
        "input_uncached_tokens": 100,
        "input_cache_read_tokens": 0,
        "input_cache_write_tokens": 0,
        "output_tokens": 10,
        "usage_completeness": UsageCompleteness.complete,
        "source_event_ids": [f"ev_{cid}_s", f"ev_{cid}_f"],
    }
    data.update(kw)
    return ModelCall.model_validate(data)


def item(cid: str, cost: str, resource: Resource = Resource.input_uncached) -> CostLineItem:
    return CostLineItem(
        line_item_id=f"li_{cid}_{resource.value}",
        dataset_id="d",
        call_id=cid,
        pricing_run_id="pr",
        resource=resource,
        data_kind=DataKind.synthetic,
        quantity=100,
        unit_quantity=1_000_000,
        unit_price=Decimal("1"),
        currency="USD",
        line_cost=Decimal(cost),
        status=LineItemStatus.priced,
        evidence_class=EvidenceClass.B,
    )


def outcome(run: str, status: OutcomeStatus, dispositions: list[CallDisposition]) -> Outcome:
    return Outcome(
        dataset_id="d",
        workflow_run_id=run,
        workflow_id="wf",
        data_kind=DataKind.synthetic,
        status=status,
        success=status is OutcomeStatus.succeeded,
        outcome_source=OutcomeSource.synthetic_driver,
        terminal_at_ms=T0 + 60_000,
        call_dispositions=dispositions,
        source_event_ids=[f"ev_{run}_o"],
    )


def test_one_dollar_call_hit_by_three_detectors_is_flagged_once(V20: None = None) -> None:
    # run_x failed; call c1 was a paid attempt superseded by retry c2 AND is itself a fallback
    # of c0 whose result was discarded. Same $1 line item is referenced by all three findings.
    calls = [
        call(
            "c0",
            "run_x",
            "answer",
            1,
            provider=Provider.anthropic,
            model_requested="fixture-anthropic-v1",
            model_resolved="fixture-anthropic-v1",
        ),
        call("c1", "run_x", "answer", 2, fallback_of_call_id="c0"),
        call("c2", "run_x", "answer", 3, retry_of_call_id="c1"),
    ]
    inputs = DetectorInputs(
        dataset_id="d",
        calls=calls,
        outcomes={
            "run_x": outcome(
                "run_x",
                OutcomeStatus.failed,
                [
                    CallDisposition(
                        call_id="c0", disposition=Disposition.discarded, reason="attempt_failed"
                    ),
                    CallDisposition(
                        call_id="c1",
                        disposition=Disposition.discarded,
                        reason="superseded_by_retry",
                    ),
                    CallDisposition(
                        call_id="c2", disposition=Disposition.used, reason="used_as_final"
                    ),
                ],
            )
        },
        items_by_call={
            "c0": [item("c0", "0.25")],
            "c1": [item("c1", "1")],
            "c2": [item("c2", "0.5")],
        },
    )
    findings = (
        detect_discarded_attempts(inputs)
        + detect_failed_runs(inputs)
        + detect_unused_fallbacks(inputs)
    )
    on_c1 = sorted(
        {f.detector.value for f in findings if "li_c1_input_uncached" in f.line_item_ids}
    )
    assert on_c1 == [
        "fallback_waste.unused_fallback",
        "retry_waste.discarded_attempt",
        "retry_waste.failed_run",
    ]
    summary = summarize(inputs, findings)
    assert summary.unique_flagged_cost_usd == Decimal("1.75")  # c0 + c1 + c2 once each, not 3x
    # nothing is labeled removable, so no finding may claim savings
    assert all(f.modeled_savings_usd is None for f in findings)
    assert summary.best_single_action_savings_usd is None
    assert summary.joint_savings == "not computed"


def test_used_or_unlabeled_fallbacks_never_produce_removable_savings(V21: None = None) -> None:
    calls = [
        call("p", "run_a", "answer", 1),
        call("f_used", "run_a", "answer", 2, fallback_of_call_id="p"),
        call("p2", "run_b", "answer", 1),
        call("f_unknown", "run_b", "answer", 2, fallback_of_call_id="p2"),
        call("p3", "run_c", "answer", 1),
        call("f_removable", "run_c", "answer", 2, fallback_of_call_id="p3"),
    ]
    inputs = DetectorInputs(
        dataset_id="d",
        calls=calls,
        outcomes={
            "run_a": outcome(
                "run_a",
                OutcomeStatus.succeeded,
                [CallDisposition(call_id="f_used", disposition=Disposition.used)],
            ),
            "run_b": outcome(
                "run_b",
                OutcomeStatus.succeeded,
                [CallDisposition(call_id="f_unknown", disposition=Disposition.unknown)],
            ),
            "run_c": outcome(
                "run_c",
                OutcomeStatus.succeeded,
                [
                    CallDisposition(
                        call_id="f_removable",
                        disposition=Disposition.discarded,
                        reason="redundant_fallback",
                        removal_safe_in_scenario=True,
                    )
                ],
            ),
        },
        items_by_call={c.call_id: [item(c.call_id, "0.2")] for c in calls},
    )
    findings = detect_unused_fallbacks(inputs)
    by_call = {f.call_ids[0]: f for f in findings}
    assert "f_used" not in by_call
    assert by_call["f_unknown"].modeled_savings_usd is None
    assert by_call["f_unknown"].evidence_level.value == "insufficient"
    assert by_call["f_removable"].modeled_savings_usd == Decimal("0.2")
    assert by_call["f_removable"].evidence_level.value == "labeled"
    summary = summarize(inputs, findings)
    assert summary.best_single_action_savings_usd == Decimal("0.2")
    assert summary.best_single_action_finding_id == by_call["f_removable"].finding_id


def test_cost_per_successful_outcome_and_zero_success(V22: None = None) -> None:
    calls = []
    outcomes = {}
    items = {}
    for i in range(100):
        run = f"run_{i:03d}"
        cid = f"c{i}"
        calls.append(call(cid, run, "answer", 1))
        items[cid] = [item(cid, "0.1")]
        status = OutcomeStatus.succeeded if i < 90 else OutcomeStatus.failed
        outcomes[run] = outcome(run, status, [])
    inputs = DetectorInputs(dataset_id="d", calls=calls, outcomes=outcomes, items_by_call=items)
    econ = outcome_economics(inputs)
    assert econ.cohort_runs == 100 and econ.succeeded == 90 and econ.failed == 10
    assert econ.cohort_known_cost_usd == Decimal("10")
    assert econ.cost_per_successful_outcome_usd == (Decimal("10") / 90).quantize(
        Decimal("1.000000000000")
    )
    assert econ.successful_runs_only_cost_usd == Decimal("9")
    assert econ.is_lower_bound is False and econ.note == "complete"
    none_succeeded = DetectorInputs(
        dataset_id="d",
        calls=calls[:2],
        outcomes={r: outcome(r, OutcomeStatus.failed, []) for r in ("run_000", "run_001")},
        items_by_call=items,
    )
    zero = outcome_economics(none_succeeded)
    assert zero.cost_per_successful_outcome_usd is None and zero.note == "no successful outcomes"


def test_unknown_cost_in_cohort_marks_lower_bound(V26: None = None) -> None:
    calls = [
        call("ok", "run_1", "answer", 1),
        call(
            "missing",
            "run_1",
            "answer",
            2,
            usage_completeness=UsageCompleteness.missing,
            input_total_tokens=None,
            input_uncached_tokens=None,
            input_cache_read_tokens=None,
            input_cache_write_tokens=None,
            output_tokens=None,
            status=CallStatus.timeout,
        ),
    ]
    inputs = DetectorInputs(
        dataset_id="d",
        calls=calls,
        outcomes={"run_1": outcome("run_1", OutcomeStatus.succeeded, [])},
        items_by_call={"ok": [item("ok", "0.5")]},
    )
    econ = outcome_economics(inputs)
    assert econ.is_lower_bound is True and econ.unknown_cost_calls == 1
    assert econ.cost_per_successful_outcome_usd == Decimal("0.5")
    assert econ.note == "known-cost lower bound"


def test_demo_findings_match_expected_metrics(tmp_path: Path) -> None:
    expected = load_expected_metrics()
    ws = Workspace(tmp_path / "ws")
    ws.init(data_kind=DataKind.synthetic, now_ms=1, aiecon_version=__version__)
    with Storage.open(ws.db_path) as storage:
        ingest_paths(
            storage,
            [fixture_root() / "events.jsonl"],
            now_ms=2,
            workspace_data_kind=DataKind.synthetic,
        )
        run_pricing(storage, "demo-support-v1", load_synthetic_catalog(), now_ms=3)
        inputs = load_inputs(storage, "demo-support-v1")
    context = analyze(
        inputs.calls, CatalogIndex(load_synthetic_catalog()), dataset_id="demo-support-v1"
    )
    findings = (
        detect_discarded_attempts(inputs)
        + detect_failed_runs(inputs)
        + detect_unused_fallbacks(inputs)
        + detect_repeated_context(inputs, context)
    )
    summary = summarize(inputs, findings)
    assert summary.by_detector["retry_waste.failed_run"] == 10
    # 5 paid superseded attempts, 3 timed-out attempts and the 2 failed primaries that were
    # superseded by a fallback are discarded attempts (the latter five with unknown cost)
    assert summary.by_detector["retry_waste.discarded_attempt"] == 10
    # 3 redundant fallbacks are findings; the 2 used fallbacks are not
    assert summary.by_detector["fallback_waste.unused_fallback"] == 3
    # groups A and C are beneficial; B is already cached and D is not beneficial
    assert summary.by_detector["repeated_context"] == 2
    # the generator's "discarded paid attempts" are the 5 superseded retries plus the 3
    # redundant fallbacks, i.e. every discarded call that carried usage
    discarded_paid = [
        f
        for f in findings
        if f.detector.value in ("retry_waste.discarded_attempt", "fallback_waste.unused_fallback")
        and f.observed_cost_complete
    ]
    assert len(discarded_paid) == expected["waste"]["discarded_paid_attempts"] == 8
    assert sum(f.observed_cost_usd for f in discarded_paid) == Decimal(
        expected["waste"]["discarded_paid_cost_usd"]
    )
    failed = [f for f in findings if f.detector.value == "retry_waste.failed_run"]
    assert sum(f.observed_cost_usd for f in failed) == Decimal(
        expected["waste"]["failed_run_known_cost_usd"]
    )
    assert (
        sum(f.unknown_cost_call_count for f in failed)
        == expected["waste"]["failed_run_unknown_cost_calls"]
    )
    # the union of flagged line items is priced once; the context findings add the input side
    # of 47 repeated-prefix calls, so the union is at least the waste-only expectation
    assert summary.unique_flagged_cost_usd >= Decimal(expected["waste"]["unique_flagged_cost_usd"])
    waste_only = summarize(inputs, [f for f in findings if f.detector.value != "repeated_context"])
    assert waste_only.unique_flagged_cost_usd == Decimal(
        expected["waste"]["unique_flagged_cost_usd"]
    )
    best = next(f for f in findings if f.finding_id == summary.best_single_action_finding_id)
    assert best.detector.value == "repeated_context"
    assert summary.best_single_action_savings_usd == Decimal(
        expected["context_economics"]["group_a_uncached_repeat"]["ephemeral_5m"]["savings_usd"]
    )
    econ = outcome_economics(inputs)
    assert econ.succeeded == 90 and econ.failed == 10
    assert econ.cost_per_successful_outcome_usd == Decimal(
        expected["cost_per_successful_outcome_usd"]
    )
    assert econ.is_lower_bound is True and econ.unknown_cost_calls == 5
