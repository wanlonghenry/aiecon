"""T1.1 contract tests: money as strings, UTC ms, null semantics, typed envelopes."""

from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aiecon.spec import (
    Adjustment,
    ApiFamily,
    CallStatus,
    ComparisonKind,
    CostLineItem,
    DataKind,
    EvidenceClass,
    LineItemStatus,
    ModelCall,
    Outcome,
    OutcomeSource,
    OutcomeStatus,
    PriceCatalog,
    Provider,
    ProviderRecord,
    RawEnvelope,
    ReconciliationBucket,
    ReconciliationStatus,
    RecordKind,
    Resource,
    UsageCompleteness,
    UsageFormat,
)
from aiecon.spec.schema_export import export_json_schemas

PLAN_EXAMPLE = {
    "schema_version": "0.1",
    "event_id": "ev_demo_call_001_finished_1",
    "event_type": "call_finished",
    "revision": 1,
    "dataset_id": "demo-support-v1",
    "data_kind": "synthetic",
    "source_type": "litellm_callback",
    "source_version": "pinned-at-build",
    "observed_at_ms": 1790467201000,
    "occurred_at_ms": 1790467200000,
    "context": {
        "workflow_id": "support_agent",
        "workflow_run_id": "run_001",
        "node_id": "reasoning",
        "node_run_id": "node_001",
        "call_id": "call_001",
        "scope_id": "demo_scope_a",
        "attempt_index": 1,
    },
    "payload": {
        "provider": "openai",
        "model_requested": "fixture-openai",
        "model_resolved": "fixture-openai-v1",
        "status": "success",
        "usage_format": "openai_responses",
        "usage": {
            "input_tokens": 1200,
            "input_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 0},
            "output_tokens": 100,
        },
    },
}


def test_plan_example_envelope_parses_with_typed_payload() -> None:
    env = RawEnvelope.model_validate(PLAN_EXAMPLE)
    assert env.payload.status is CallStatus.success
    assert env.payload.usage_format is UsageFormat.openai_responses
    assert env.payload.usage["input_tokens_details"]["cached_tokens"] == 800
    # round trip through JSON keeps the same content hash
    again = RawEnvelope.model_validate_json(env.model_dump_json())
    assert again.content_hash() == env.content_hash()


def test_content_hash_ignores_observed_at_but_not_content() -> None:
    base = RawEnvelope.model_validate(PLAN_EXAMPLE)
    redelivered = copy.deepcopy(PLAN_EXAMPLE)
    redelivered["observed_at_ms"] += 5000
    assert RawEnvelope.model_validate(redelivered).content_hash() == base.content_hash()
    changed = copy.deepcopy(PLAN_EXAMPLE)
    changed["payload"]["usage"]["output_tokens"] = 101
    assert RawEnvelope.model_validate(changed).content_hash() != base.content_hash()


def test_payload_type_follows_event_type() -> None:
    wrong = copy.deepcopy(PLAN_EXAMPLE)
    wrong["event_type"] = "call_started"  # started payloads have no status field
    with pytest.raises(ValidationError):
        RawEnvelope.model_validate(wrong)


def test_usage_rejects_non_integer_values_without_echoing_them() -> None:
    canary = "CANARY-PROMPT-TEXT-9f8e7d"
    bad = copy.deepcopy(PLAN_EXAMPLE)
    bad["payload"]["usage"]["input_tokens"] = canary
    with pytest.raises(ValidationError) as excinfo:
        RawEnvelope.model_validate(bad)
    # error types/locations are fine; the value itself must not appear in our message
    messages = " ".join(err["msg"] for err in excinfo.value.errors())
    assert canary not in messages


def test_unknown_fields_are_rejected_not_dropped() -> None:
    bad = copy.deepcopy(PLAN_EXAMPLE)
    bad["payload"]["messages"] = [{"role": "user", "content": "hello"}]
    with pytest.raises(ValidationError):
        RawEnvelope.model_validate(bad)


def test_ids_must_be_opaque_tokens() -> None:
    bad = copy.deepcopy(PLAN_EXAMPLE)
    bad["context"]["workflow_id"] = "support agent with spaces"
    with pytest.raises(ValidationError):
        RawEnvelope.model_validate(bad)


def test_outcome_envelope_requires_run_id_and_terminal_time() -> None:
    outcome = {
        "schema_version": "0.1",
        "event_id": "ev_outcome_run_001",
        "event_type": "outcome",
        "dataset_id": "demo-support-v1",
        "data_kind": "synthetic",
        "source_type": "synthetic_generator",
        "source_version": "gen-0.1",
        "observed_at_ms": 1790467300000,
        "occurred_at_ms": 1790467300000,
        "context": {"workflow_id": "support_agent", "workflow_run_id": "run_001", "scope_id": "s"},
        "payload": {
            "status": "succeeded",
            "success": True,
            "outcome_source": "synthetic_driver",
            "terminal_at_ms": 1790467300000,
            "call_dispositions": [
                {"call_id": "call_001", "disposition": "used", "reason": "used_as_final"}
            ],
        },
    }
    env = RawEnvelope.model_validate(outcome)
    assert env.payload.call_dispositions[0].reason == "used_as_final"
    missing_ts = copy.deepcopy(outcome)
    del missing_ts["payload"]["terminal_at_ms"]
    with pytest.raises(ValidationError):
        RawEnvelope.model_validate(missing_ts)
    prose_reason = copy.deepcopy(outcome)
    prose_reason["payload"]["call_dispositions"][0]["reason"] = "The model said something odd"
    with pytest.raises(ValidationError):
        RawEnvelope.model_validate(prose_reason)


def _call(**overrides: object) -> ModelCall:
    data: dict[str, object] = {
        "dataset_id": "demo",
        "call_id": "c1",
        "data_kind": DataKind.synthetic,
        "scope_id": "s",
        "normalizer_version": "0.1.0",
        "provider": Provider.openai,
        "model_requested": "fixture-openai",
        "status": CallStatus.success,
    }
    data.update(overrides)
    return ModelCall.model_validate(data)


def test_model_call_input_identity() -> None:
    ok = _call(
        input_total_tokens=1000,
        input_uncached_tokens=300,
        input_cache_read_tokens=600,
        input_cache_write_tokens=100,
        output_tokens=100,
        usage_completeness=UsageCompleteness.complete,
    )
    assert ok.input_total_tokens == 1000
    with pytest.raises(ValidationError):
        _call(
            input_total_tokens=1000,
            input_uncached_tokens=300,
            input_cache_read_tokens=600,
            input_cache_write_tokens=200,
        )


def test_unknown_usage_stays_null() -> None:
    call = _call(status=CallStatus.timeout, usage_completeness=UsageCompleteness.missing)
    assert call.input_total_tokens is None
    assert call.output_tokens is None
    dumped = json.loads(call.model_dump_json())
    assert dumped["input_total_tokens"] is None
    assert dumped["usage_completeness"] == "missing"


def test_money_serializes_as_decimal_string() -> None:
    item = CostLineItem(
        line_item_id="li_1",
        dataset_id="demo",
        call_id="c1",
        pricing_run_id="pr_1",
        resource=Resource.input_uncached,
        data_kind=DataKind.synthetic,
        quantity=300,
        unit_quantity=1_000_000,
        unit_price=Decimal("2"),
        currency="USD",
        line_cost=Decimal("0.000600"),
        status=LineItemStatus.priced,
        evidence_class=EvidenceClass.B,
        catalog_version="synthetic-0.1",
        price_id="p1",
    )
    dumped = json.loads(item.model_dump_json())
    assert dumped["line_cost"] == "0.000600"
    assert dumped["unit_price"] == "2"
    assert isinstance(item.line_cost, Decimal)
    tiny = CostLineItem.model_validate({**item.model_dump(), "line_cost": "1E-7"})
    assert json.loads(tiny.model_dump_json())["line_cost"] == "0.0000001"


def test_unpriced_line_item_cannot_carry_cost() -> None:
    with pytest.raises(ValidationError):
        CostLineItem(
            line_item_id="li_2",
            dataset_id="demo",
            call_id="c1",
            pricing_run_id="pr_1",
            resource=Resource.output,
            data_kind=DataKind.synthetic,
            quantity=10,
            status=LineItemStatus.unpriced,
            unpriced_reason="unknown_model",
            line_cost=Decimal("0"),
            evidence_class=EvidenceClass.B,
        )


def test_synthetic_catalog_only_prices_fixture_models() -> None:
    record = {
        "price_id": "p1",
        "catalog_version": "synthetic-0.1",
        "provider": "openai",
        "model_id": "gpt-real-model",
        "resource": "output",
        "effective_from_ms": 0,
        "unit": "token",
        "unit_price": "8",
        "source_url": "synthetic://fixture",
        "retrieved_at": "2026-09-26",
        "effective_date_basis": "synthetic",
    }
    with pytest.raises(ValidationError):
        PriceCatalog(
            catalog_version="synthetic-0.1",
            catalog_kind="synthetic",
            description="test",
            records=[record],
        )
    record["model_id"] = "fixture-openai-v1"
    catalog = PriceCatalog(
        catalog_version="synthetic-0.1",
        catalog_kind="synthetic",
        description="test",
        records=[record],
    )
    assert len(catalog.catalog_hash()) == 64


def test_provider_record_kind_shapes() -> None:
    base = {
        "snapshot_id": "snap_1",
        "record_id": "r1",
        "provider": "anthropic",
        "scope_id": "ws_a",
        "record_kind": "provider_cost",
        "data_kind": "synthetic",
        "window_start_ms": 0,
        "window_end_ms": 86_400_000,
        "grain": "1d/workspace_id",
        "dimensions": {"workspace_id": None, "model": "not_grouped"},
        "amount_original": "123.45",
        "amount_unit": "cents",
        "currency": "USD",
        "amount_usd": "1.2345",
        "source_ref": "GET /v1/organizations/cost_report",
        "source_hash": "abc",
        "fetched_at_ms": 100,
        "finality": "provisional",
        "snapshot_complete": True,
    }
    rec = ProviderRecord.model_validate(base)
    assert rec.amount_usd == Decimal("1.2345")
    assert rec.dimensions["workspace_id"] is None  # default workspace stays distinct
    usage_with_amount = {**base, "record_kind": "provider_usage"}
    with pytest.raises(ValidationError):
        ProviderRecord.model_validate(usage_with_amount)
    cost_without_amount = {**base}
    del cost_without_amount["amount_original"]
    del cost_without_amount["amount_usd"]
    with pytest.raises(ValidationError):
        ProviderRecord.model_validate(cost_without_amount)
    assert RecordKind.settled_cost.value == "settled_cost"


def test_reconciliation_algebra_holds() -> None:
    bucket = ReconciliationBucket(
        reconcile_run_id="rr_1",
        bucket_key="openai/day1",
        comparison_kind=ComparisonKind.cost_comparison,
        dataset_id="demo",
        data_kind=DataKind.synthetic,
        provider=Provider.openai,
        scope_id="proj_a",
        window_start_ms=0,
        window_end_ms=86_400_000,
        grain="1d/project_id",
        local_estimate_usd=Decimal("1.10"),
        provider_cost_usd=Decimal("1.00"),
        signed_variance_usd=Decimal("0.10"),
        absolute_variance_usd=Decimal("0.10"),
        variance_pct=Decimal("10"),
        status=ReconciliationStatus.variance,
        explained_adjustments=[
            Adjustment(
                code="unpriced_calls",
                signed_amount_usd=Decimal("0.04"),
                evidence_backed=True,
                evidence_refs=["c9"],
                description="one call priced locally but absent from provider usage",
            )
        ],
        hypotheses=[
            Adjustment(
                code="late_data",
                signed_amount_usd=Decimal("0.06"),
                evidence_backed=False,
                description="provider data may still be provisional",
            )
        ],
        unexplained_delta_usd=Decimal("0.06"),
    )
    assert bucket.unexplained_delta_usd == Decimal("0.06")
    with pytest.raises(ValidationError):
        ReconciliationBucket.model_validate(
            {**bucket.model_dump(), "unexplained_delta_usd": "0.10"}
        )


def test_outcome_projection_helpers() -> None:
    outcome = Outcome(
        dataset_id="demo",
        workflow_run_id="run_1",
        data_kind=DataKind.synthetic,
        status=OutcomeStatus.pending,
        outcome_source=OutcomeSource.synthetic_driver,
    )
    assert not outcome.is_terminal
    assert outcome.disposition_for("nope") is None


def test_schema_export_marks_runtime_experimental(tmp_path) -> None:
    written = export_json_schemas(tmp_path)
    names = {p.name for p in written}
    assert "RawEnvelope.schema.json" in names
    assert "CostLineItem.schema.json" in names
    runtime = json.loads((tmp_path / "RuntimeExecution.schema.json").read_text())
    assert runtime["x-experimental"] is True
    core = json.loads((tmp_path / "CostLineItem.schema.json").read_text())
    assert "x-experimental" not in core
    line_cost = core["properties"]["line_cost"]
    assert {"type": "string"} in line_cost["anyOf"]
    assert {"type": "null"} in line_cost["anyOf"]
    assert ApiFamily.messages.value == "messages"
