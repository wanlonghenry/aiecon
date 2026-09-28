"""The OpenAI dashboard cost export converts to a provider_cost snapshot aiecon imports."""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

from aiecon import __version__
from aiecon.billing.importer import import_snapshot
from aiecon.spec import DataKind
from aiecon.storage import Storage, Workspace

SCRIPT = Path(__file__).resolve().parents[2] / "examples" / "openai_dashboard_cost_to_aiecon.py"
D1 = 1_790_553_600_000  # 2026-09-28T00:00:00Z
DAY = 86_400_000

HEADER = (
    "start_time,end_time,start_time_iso,end_time_iso,amount_value,amount_currency,quantity,"
    "quantity_unit,line_item,user_id,api_key_id,project_id,organization_id,project_name,"
    "organization_name,user_email,api_source\n"
)
CSV_TEXT = HEADER + (
    "1790467200,1790553600,2026-09-27T00:00:00+00:00,2026-09-28T00:00:00+00:00,,,,,,,,,,,,,\n"
    "1790553600,1790640000,2026-09-28T00:00:00,2026-09-29T00:00:00,"
    "0.003835980000000000000000000000,usd,,,,,,,org-fixture,,Fixture Org,someone@example.com,\n"
    "1790553600,1790640000,2026-09-28T00:00:00,2026-09-29T00:00:00,"
    '0.5,usd,1000,tokens,"gpt-5-nano, input_tokens",,key_1,proj_a,org-fixture,Proj A,'
    "someone@example.com,\n"
)


def load_module():
    spec = importlib.util.spec_from_file_location("dashboard_cost", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_dashboard_rows_become_cost_records_without_names_or_emails(tmp_path: Path) -> None:
    module = load_module()
    csv_path = tmp_path / "cost.csv"
    csv_path.write_text(CSV_TEXT, encoding="utf-8", newline="\n")
    records, dims, skipped = module.convert_rows(module.read_csv(csv_path))
    assert skipped == 1 and len(records) == 2
    org_total, grouped = records
    assert org_total["window_start_ms"] == D1 and org_total["window_end_ms"] == D1 + DAY
    assert org_total["amount_original"] == "0.003835980000000000000000000000"
    assert org_total["amount_unit"] == "usd" and org_total["currency"] == "USD"
    assert org_total["dimensions_json"] == {
        "organization_id": "org-fixture",
        "line_item": "not_grouped",
        "project_id": "not_grouped",
    }
    assert grouped["dimensions_json"]["project_id"] == "proj_a"
    assert grouped["dimensions_json"]["line_item"] == "gpt-5-nano, input_tokens"
    assert grouped["usage_json"] == {"quantity_tokens": 1000}
    text = str(records)
    assert "example.com" not in text and "Fixture Org" not in text and "Proj A" not in text
    assert "line_item" in dims and "project_id" in dims


def test_converted_cost_snapshot_imports_with_usd_amounts(tmp_path: Path) -> None:
    module = load_module()
    csv_path = tmp_path / "cost.csv"
    csv_path.write_text(HEADER + CSV_TEXT.splitlines()[2] + "\n", encoding="utf-8", newline="\n")
    records, dims, _skipped = module.convert_rows(module.read_csv(csv_path))
    records_path, manifest_path = module.write_snapshot(
        records,
        dims,
        out_dir=tmp_path / "snap",
        snapshot_id="snap_dashboard_test",
        scope_id="demo_scope_openai",
        source_ref="OpenAI dashboard cost export test.csv",
        query_window=(D1, D1 + DAY),
        fetched_at_ms=D1 + DAY,
        dedicated=True,
        data_kind="synthetic",
    )
    ws = Workspace(tmp_path / "ws")
    ws.init(data_kind=DataKind.synthetic, now_ms=1, aiecon_version=__version__)
    with Storage.open(ws.db_path) as storage:
        result = import_snapshot(
            storage,
            file_path=records_path,
            manifest_path=manifest_path,
            now_ms=2,
            workspace_data_kind=DataKind.synthetic,
        )
        assert result.record_count == 1
        assert result.total_amount_usd == Decimal("0.003835980000000000000000000000")
        record = storage.list_provider_records()[0]
        assert record.record_kind.value == "provider_cost" and record.amount_usd == Decimal(
            "0.00383598"
        )
        assert storage.list_snapshots()[0].grain == "1d/organization_id"
