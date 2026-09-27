"""T1.3: the committed fixtures are exactly what the generator produces, and they ingest."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from aiecon import __version__
from aiecon.data import synthetic
from aiecon.ingest import ingest_paths
from aiecon.pipeline import fixture_root, load_expected_metrics
from aiecon.spec import CallStatus, DataKind, PriceCatalog, UsageCompleteness
from aiecon.storage import Storage, Workspace


def test_committed_fixtures_match_generator_byte_for_byte() -> None:
    rendered = synthetic.render_files(synthetic.generate())
    root = fixture_root()
    for relative, text in rendered.items():
        on_disk = (root / relative).read_text(encoding="utf-8")
        assert on_disk == text, f"{relative} differs from generator output"


def test_expected_metrics_shape_from_plan() -> None:
    expected = load_expected_metrics()
    assert expected["calls"] == 313
    assert expected["call_events"] == 626
    assert expected["outcome_events"] == 100
    assert expected["succeeded_runs"] == 90 and expected["failed_runs"] == 10
    assert expected["retry_runs"] == 8 and expected["fallback_runs"] == 5
    assert expected["calls_with_missing_usage"] == 5  # 3 timeouts + 2 failed primaries
    total = Decimal(expected["estimated_known_cost_usd"])
    assert Decimal(expected["cost_per_successful_outcome_usd"]) == (total / 90).quantize(
        Decimal("1.000000000000")
    )
    assert expected["cost_per_successful_outcome_is_lower_bound"] is True
    # provider fixtures: OpenAI matches local exactly, Anthropic deviates on purpose
    by_day = expected["estimated_cost_by_provider_day_usd"]
    provider = expected["provider_cost_usd"]
    for day in ("2026-09-26", "2026-09-27"):
        assert Decimal(provider["openai"][day]) == Decimal(by_day[f"openai/{day}"])
    phantom = Decimal(expected["phantom_call_cost_usd"])
    assert phantom == Decimal("0.072")  # 20000 x $2/M + 4000 x $8/M
    assert Decimal(provider["anthropic"]["2026-09-26"]) == (
        Decimal(by_day["anthropic/2026-09-26"]) + phantom
    )
    assert Decimal(provider["anthropic"]["2026-09-27"]) == (
        Decimal(by_day["anthropic/2026-09-27"]) + Decimal(expected["unexplained_residue_usd"])
    )


def test_context_scenarios_follow_section_8_3_formula() -> None:
    expected = load_expected_metrics()["context_economics"]
    group_a = expected["group_a_uncached_repeat"]
    # runs 1-40 plus the extra attempts of runs 3, 17 and 31; run 17's first attempt timed out
    assert group_a["appearances"] == 43 and group_a["calls_with_usage"] == 42
    # 42 x 3000 x $2/M = 0.252 ; 3000 x $2.5/M + 41 x 3000 x $0.5/M = 0.0075 + 0.0615
    assert Decimal(group_a["ephemeral_5m"]["no_cache_cost_usd"]) == Decimal("0.252")
    assert Decimal(group_a["ephemeral_5m"]["cache_cost_usd"]) == Decimal("0.069")
    assert Decimal(group_a["ephemeral_5m"]["savings_usd"]) == Decimal("0.183")
    group_c = expected["group_c_cross_ttl"]
    assert group_c["calls_with_usage"] == 5
    # every call 8 minutes apart: five cold 5m writes lose, one 1h write plus four reads wins
    assert Decimal(group_c["ephemeral_5m"]["savings_usd"]) == Decimal("-0.0075")
    assert Decimal(group_c["ephemeral_1h"]["savings_usd"]) == Decimal("0.012")
    group_d = expected["group_d_single_call"]
    assert Decimal(group_d["ephemeral_5m"]["savings_usd"]) == Decimal("-0.00075")
    assert group_d["expected_recommendation"] == "not_beneficial"
    assert expected["group_b_already_cached"]["observed_cache_write_tokens"] == 2500
    assert expected["reasoning_calls_without_prefix_evidence"] == 26


def test_synthetic_catalog_is_valid_and_synthetic() -> None:
    catalog = PriceCatalog.model_validate_json(
        (fixture_root() / "synthetic-catalog.json").read_text("utf-8")
    )
    assert catalog.catalog_kind == "synthetic"
    assert all(r.model_id.startswith("fixture-") for r in catalog.records)
    assert len(catalog.records) == 8


def test_fixture_events_ingest_to_expected_counts(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    ws.init(data_kind=DataKind.synthetic, now_ms=1, aiecon_version=__version__)
    with Storage.open(ws.db_path) as storage:
        stats = ingest_paths(
            storage,
            [fixture_root() / "events.jsonl"],
            now_ms=2,
            workspace_data_kind=DataKind.synthetic,
        )
        assert stats.rejected == 0 and stats.conflicts == 0 and stats.duplicates == 0
        assert stats.accepted == 726
        assert storage.count_calls(synthetic.DATASET_ID) == 313
        assert storage.count_outcomes(synthetic.DATASET_ID) == 100
        calls = storage.list_calls(synthetic.DATASET_ID)
        assert sum(1 for c in calls if c.status is CallStatus.success) == 308
        assert sum(1 for c in calls if c.usage_completeness is UsageCompleteness.complete) == 308
        assert sum(1 for c in calls if c.retry_of_call_id) == 8
        assert sum(1 for c in calls if c.fallback_of_call_id) == 5
        boundary = [c for c in calls if c.started_at_ms < synthetic.DAY2_MS <= (c.ended_at_ms or 0)]
        assert [c.call_id for c in boundary] == ["call_055_rsn_1"]
        outcomes = storage.list_outcomes(synthetic.DATASET_ID)
        assert sum(1 for o in outcomes if o.success) == 90
        # every call is mentioned in its run's dispositions
        disposition_ids = {d.call_id for o in outcomes for d in o.call_dispositions}
        assert disposition_ids == {c.call_id for c in calls}
        # the events file itself must not carry content-bearing keys
        raw = (fixture_root() / "events.jsonl").read_text("utf-8")
        for forbidden_key in ('"messages":', '"prompt":', '"content":', '"choices":', '"text":'):
            assert forbidden_key not in raw
        assert json.loads(raw.splitlines()[0])["data_kind"] == "synthetic"
