"""T3.1 / V13 / V15: unit conversion, idempotent snapshot import, per-day snapshot selection."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from aiecon import __version__
from aiecon.billing import BillingImportError, UnsupportedAmountError, import_snapshot, to_usd
from aiecon.pipeline import fixture_root, load_expected_metrics
from aiecon.spec import DataKind
from aiecon.storage import Storage, Workspace

DAY = 86_400_000
D1 = 1_790_380_800_000


def test_cents_string_converts_exactly_and_unknown_units_are_refused() -> None:
    assert to_usd(Decimal("123.45"), "cents", "USD") == Decimal("1.2345")
    assert to_usd(Decimal("0.06"), "usd", "usd") == Decimal("0.06")
    with pytest.raises(UnsupportedAmountError):
        to_usd(Decimal("1"), "credits", "USD")
    with pytest.raises(UnsupportedAmountError):
        to_usd(Decimal("1"), "cents", "EUR")


@pytest.fixture
def synthetic_ws(tmp_path: Path) -> Workspace:
    ws = Workspace(tmp_path / "ws")
    ws.init(data_kind=DataKind.synthetic, now_ms=1, aiecon_version=__version__)
    return ws


def import_fixture(storage: Storage, name: str):
    root = fixture_root() / "provider"
    return import_snapshot(
        storage,
        file_path=root / f"{name}.json",
        manifest_path=root / f"{name}.manifest.json",
        now_ms=5,
        workspace_data_kind=DataKind.synthetic,
    )


def test_synthetic_snapshots_import_and_reimport_is_a_noop(synthetic_ws: Workspace) -> None:
    expected = load_expected_metrics()
    with Storage.open(synthetic_ws.db_path) as storage:
        results = {
            n: import_fixture(storage, n)
            for n in ("openai_usage", "openai_cost", "anthropic_usage", "anthropic_cost")
        }
        assert results["openai_usage"].record_count == 2
        assert results["openai_cost"].record_count == 6
        assert results["anthropic_usage"].record_count == 2
        assert results["anthropic_cost"].record_count == 8
        assert all(r.non_usd_records == 0 for r in results.values())
        anthropic_total = results["anthropic_cost"].total_amount_usd
        expected_anthropic = sum(
            Decimal(v) for v in expected["provider_cost_usd"]["anthropic"].values()
        )
        assert anthropic_total == expected_anthropic  # cents strings converted exactly

        again = import_fixture(storage, "anthropic_cost")
        assert again.skipped_same_hash is True
        active = storage.query("SELECT COUNT(*) FROM meta_snapshots WHERE active")[0][0]
        assert active == 4
        total = storage.query(
            "SELECT SUM(amount_usd) FROM current_provider_records "
            "WHERE record_kind = 'provider_cost' AND provider = 'anthropic'"
        )[0][0]
        assert Decimal(total) == expected_anthropic

        # usage records were normalized and kept the provider-native counters as evidence
        records = [
            r for r in storage.list_provider_records() if r.record_kind.value == "provider_usage"
        ]
        openai_day1 = next(
            r for r in records if r.provider.value == "openai" and r.window_start_ms == D1
        )
        assert openai_day1.usage["input_total_tokens"] == (
            openai_day1.usage["input_uncached_tokens"]
            + openai_day1.usage["input_cache_read_tokens"]
            + openai_day1.usage["input_cache_write_tokens"]
        )
        assert openai_day1.usage_native["num_model_requests"] == openai_day1.usage["requests"]
        anthropic_day1 = next(
            r for r in records if r.provider.value == "anthropic" and r.window_start_ms == D1
        )
        assert (
            "requests" not in anthropic_day1.usage
        )  # the Anthropic usage report has no request count
        assert anthropic_day1.usage_native["cache_creation.ephemeral_5m_input_tokens"] >= 0


def write_snapshot(
    tmp_path: Path, name: str, records: list[dict], *, fetched_at: int, window_end: int
) -> tuple[Path, Path]:
    body = json.dumps({"records": records}, indent=2, sort_keys=True) + "\n"
    file_path = tmp_path / f"{name}.json"
    file_path.write_text(body, encoding="utf-8", newline="\n")
    manifest = {
        "schema_version": "0.1",
        "snapshot_id": name,
        "provider": "openai",
        "scope_id": "proj_a",
        "record_kind": "provider_cost",
        "data_kind": "synthetic",
        "grain": "1d/line_item,project_id",
        "query_window": {"start_ms": D1, "end_ms": window_end},
        "fetched_at_ms": fetched_at,
        "source_ref": "synthetic://test/openai/cost",
        "source_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "finality": "provisional",
        "snapshot_complete": True,
    }
    manifest_path = tmp_path / f"{name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return file_path, manifest_path


def cost_row(record_id: str, day_ms: int, amount: str, line_item: str = "m, input_tokens") -> dict:
    return {
        "record_id": record_id,
        "window_start_ms": day_ms,
        "window_end_ms": day_ms + DAY,
        "dimensions_json": {"project_id": "proj_a", "line_item": line_item},
        "amount_original": amount,
        "amount_unit": "usd",
        "currency": "USD",
        "usage_json": None,
    }


def _import(storage: Storage, paths: tuple[Path, Path], now_ms: int = 1):
    return import_snapshot(
        storage,
        file_path=paths[0],
        manifest_path=paths[1],
        now_ms=now_ms,
        workspace_data_kind=DataKind.synthetic,
    )


def test_re_pull_supplies_only_the_days_it_covers(synthetic_ws: Workspace, tmp_path: Path) -> None:
    """G03: a later one-day pull replaces that day only; the other days keep their source."""

    from aiecon.reconcile import effective_provider_records
    from aiecon.spec import TimeWindow

    old = write_snapshot(
        tmp_path,
        "snap_old",
        [cost_row("r1", D1, "5"), cost_row("r2", D1 + DAY, "1")],
        fetched_at=10,
        window_end=D1 + 2 * DAY,
    )
    day1_again = write_snapshot(
        tmp_path, "snap_day1", [cost_row("r1", D1, "7")], fetched_at=20, window_end=D1 + DAY
    )
    with Storage.open(synthetic_ws.db_path) as storage:
        _import(storage, old)
        result = _import(storage, day1_again, now_ms=2)
        assert result.superseded_snapshot_ids == ["snap_old"]
        # every pull stays on file and active: nothing is deactivated at import time
        assert {s.snapshot_id for s in storage.list_snapshots()} == {"snap_old", "snap_day1"}
        assert storage.query("SELECT COUNT(*) FROM provider_records")[0][0] == 3
        window = TimeWindow(start_ms=D1, end_ms=D1 + 2 * DAY)
        effective = effective_provider_records(storage, data_kind=DataKind.synthetic, window=window)
        by_id = {(r.snapshot_id, r.record_id): r.amount_usd for r in effective.records}
        # day 1 from the newer pull (7, not 5 and never 12); day 2 still from the older pull
        assert by_id == {("snap_day1", "r1"): Decimal("7"), ("snap_old", "r2"): Decimal("1")}
        assert effective.snapshot_ids == ["snap_day1", "snap_old"]
        # a newer pull covering both days supplies both: rows it no longer reports vanish
        both = write_snapshot(
            tmp_path, "snap_both", [cost_row("r1", D1, "8")], fetched_at=30, window_end=D1 + 2 * DAY
        )
        _import(storage, both, now_ms=3)
        effective = effective_provider_records(storage, data_kind=DataKind.synthetic, window=window)
        assert [(r.snapshot_id, r.record_id, r.amount_usd) for r in effective.records] == [
            ("snap_both", "r1", Decimal("8"))
        ]
        # an older fetch arriving late never overrides a newer one for the same day
        late = write_snapshot(
            tmp_path, "snap_late", [cost_row("r1", D1, "6")], fetched_at=25, window_end=D1 + DAY
        )
        _import(storage, late, now_ms=4)
        effective = effective_provider_records(storage, data_kind=DataKind.synthetic, window=window)
        assert [r.amount_usd for r in effective.records] == [Decimal("8")]


def test_same_snapshot_id_is_idempotent_and_different_content_is_refused(
    synthetic_ws: Workspace, tmp_path: Path
) -> None:
    """G07: idempotency is per snapshot id; the same bytes under a new id are a new observation."""

    first = write_snapshot(
        tmp_path, "snap_x", [cost_row("r1", D1, "5")], fetched_at=10, window_end=D1 + DAY
    )
    with Storage.open(synthetic_ws.db_path) as storage:
        assert _import(storage, first).record_count == 1
        again = _import(storage, first, now_ms=2)
        assert again.skipped_same_hash is True and again.record_count == 0
        assert storage.query("SELECT COUNT(*) FROM provider_records")[0][0] == 1
        # same id, different bytes: contract error, nothing changes
        changed = write_snapshot(
            tmp_path, "snap_x", [cost_row("r1", D1, "9")], fetched_at=10, window_end=D1 + DAY
        )
        with pytest.raises(BillingImportError, match="different content"):
            _import(storage, changed, now_ms=3)
        assert storage.scalar("SELECT amount_usd FROM provider_records") == Decimal("5")
        # same bytes, new id and later fetch: registered as its own snapshot
        renamed = write_snapshot(
            tmp_path, "snap_y", [cost_row("r1", D1, "5")], fetched_at=20, window_end=D1 + DAY
        )
        result = _import(storage, renamed, now_ms=4)
        assert result.skipped_same_hash is False and result.record_count == 1
        assert result.superseded_snapshot_ids == ["snap_x"]


def test_hash_mismatch_and_data_kind_mismatch_are_contract_errors(
    synthetic_ws: Workspace, tmp_path: Path
) -> None:
    file_path, manifest_path = write_snapshot(
        tmp_path, "snap_x", [cost_row("r1", D1, "5")], fetched_at=10, window_end=D1 + DAY
    )
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["source_hash"] = "0" * 64
    bad_manifest = tmp_path / "bad.manifest.json"
    bad_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with Storage.open(synthetic_ws.db_path) as storage:
        with pytest.raises(BillingImportError, match="source_hash"):
            import_snapshot(
                storage,
                file_path=file_path,
                manifest_path=bad_manifest,
                now_ms=1,
                workspace_data_kind=DataKind.synthetic,
            )
        with pytest.raises(BillingImportError, match="data_kind"):
            import_snapshot(
                storage,
                file_path=file_path,
                manifest_path=manifest_path,
                now_ms=1,
                workspace_data_kind=DataKind.live,
            )


def test_non_usd_amounts_are_kept_but_excluded_from_usd_totals(
    synthetic_ws: Workspace, tmp_path: Path
) -> None:
    rows = [cost_row("r1", D1, "5"), {**cost_row("r2", D1, "3"), "currency": "EUR"}]
    file_path, manifest_path = write_snapshot(
        tmp_path, "snap_eur", rows, fetched_at=10, window_end=D1 + DAY
    )
    with Storage.open(synthetic_ws.db_path) as storage:
        result = import_snapshot(
            storage,
            file_path=file_path,
            manifest_path=manifest_path,
            now_ms=1,
            workspace_data_kind=DataKind.synthetic,
        )
        assert result.non_usd_records == 1
        assert result.total_amount_usd == Decimal("5")
        eur = next(r for r in storage.list_provider_records() if r.record_id == "r2")
        assert eur.amount_usd is None and eur.amount_original == Decimal("3")


def test_csv_import_matches_json_import(synthetic_ws: Workspace, tmp_path: Path) -> None:
    header = (
        "record_id,window_start_ms,window_end_ms,dimensions_json,"
        "amount_original,amount_unit,currency,usage_json\n"
    )
    dims = '"{""project_id"": ""proj_a"", ""line_item"": ""m, input_tokens""}"'
    csv_text = header + f"r1,{D1},{D1 + DAY},{dims},0.06,usd,USD,\n"
    file_path = tmp_path / "snap_csv.csv"
    file_path.write_text(csv_text, encoding="utf-8", newline="\n")
    manifest = {
        "schema_version": "0.1",
        "snapshot_id": "snap_csv",
        "provider": "openai",
        "scope_id": "proj_a",
        "record_kind": "provider_cost",
        "data_kind": "synthetic",
        "grain": "1d/line_item,project_id",
        "query_window": {"start_ms": D1, "end_ms": D1 + DAY},
        "fetched_at_ms": 10,
        "source_ref": "synthetic://test/openai/cost.csv",
        "source_hash": hashlib.sha256(file_path.read_bytes()).hexdigest(),
        "finality": "provisional",
        "snapshot_complete": True,
    }
    manifest_path = tmp_path / "snap_csv.manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with Storage.open(synthetic_ws.db_path) as storage:
        result = import_snapshot(
            storage,
            file_path=file_path,
            manifest_path=manifest_path,
            now_ms=1,
            workspace_data_kind=DataKind.synthetic,
        )
        assert result.record_count == 1 and result.total_amount_usd == Decimal("0.06")
        (record,) = storage.list_provider_records()
        assert record.dimensions == {"project_id": "proj_a", "line_item": "m, input_tokens"}
