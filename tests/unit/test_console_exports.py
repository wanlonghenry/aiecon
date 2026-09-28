"""The Anthropic Console usage export converts to a provider_usage snapshot aiecon imports."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from aiecon import __version__
from aiecon.billing.importer import import_snapshot
from aiecon.spec import DataKind
from aiecon.storage import Storage, Workspace

SCRIPT = Path(__file__).resolve().parents[2] / "examples" / "anthropic_console_usage_to_aiecon.py"
D1 = 1_790_553_600_000  # 2026-09-28T00:00:00Z
DAY = 86_400_000

CSV_TEXT = (
    "usage_date_utc,model_version,api_key,workspace,usage_type,context_window,"
    "usage_input_tokens_no_cache,usage_input_tokens_cache_write_5m,"
    "usage_input_tokens_cache_write_1h,usage_input_tokens_cache_read,usage_output_tokens,"
    "web_search_count,inference_geo,speed\n"
    "2026-09-28,fixture-anthropic-v1,demo-key,demo-ws,standard,≤ 200k,"
    "189,61624,0,61624,675,0,not_available,\n"
    "2026-09-28,fixture-anthropic-v1,demo-key,demo-ws,standard,≤ 200k,"
    "10,0,0,0,5,2,us,fast\n"
)


def load_module():
    spec = importlib.util.spec_from_file_location("console_usage", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_console_rows_map_to_the_admin_usage_shape(tmp_path: Path) -> None:
    module = load_module()
    csv_path = tmp_path / "claude_api_tokens.csv"
    csv_path.write_text(CSV_TEXT, encoding="utf-8", newline="\n")
    rows = module.read_csv(csv_path)
    records, dims = module.convert_rows(rows)
    assert dims == ["api_key_id", "context_window", "model", "service_tier", "workspace_id"] or (
        "inference_geo" in dims and "speed" in dims
    )
    first, second = records
    assert first["window_start_ms"] == D1 and first["window_end_ms"] == D1 + DAY
    assert first["dimensions_json"]["model"] == "fixture-anthropic-v1"
    assert first["dimensions_json"]["context_window"] == "<= 200k"
    assert "inference_geo" not in first["dimensions_json"]  # not_available is not a region
    assert first["usage_json"] == {
        "uncached_input_tokens": 189,
        "cache_read_input_tokens": 61624,
        "cache_creation": {"ephemeral_5m_input_tokens": 61624, "ephemeral_1h_input_tokens": 0},
        "output_tokens": 675,
    }
    assert first["amount_original"] is None  # usage, never money
    assert second["dimensions_json"]["inference_geo"] == "us"
    assert second["usage_json"]["server_tool_use"] == {"web_search_requests": 2}
    assert first["record_id"] != second["record_id"]


def test_converted_snapshot_imports_with_normalized_metrics(tmp_path: Path) -> None:
    module = load_module()
    csv_path = tmp_path / "claude_api_tokens.csv"
    csv_path.write_text(CSV_TEXT.splitlines()[0] + "\n" + CSV_TEXT.splitlines()[1] + "\n", "utf-8")
    records, dims = module.convert_rows(module.read_csv(csv_path))
    records_path, manifest_path = module.write_snapshot(
        records,
        dims,
        out_dir=tmp_path / "snap",
        snapshot_id="snap_console_test",
        scope_id="demo_scope_anthropic",
        source_ref="Anthropic Console usage export test.csv",
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
        assert result.record_count == 1 and result.total_amount_usd is None
        record = storage.list_provider_records()[0]
        assert record.record_kind.value == "provider_usage"
        assert record.usage == {
            "input_uncached_tokens": 189,
            "input_cache_read_tokens": 61624,
            "input_cache_write_tokens": 61624,
            "input_cache_write_5m_tokens": 61624,
            "input_cache_write_1h_tokens": 0,
            "output_tokens": 675,
            "input_total_tokens": 189 + 61624 + 61624,
        }
        manifest = storage.list_snapshots()[0]
        assert manifest.scope_dedicated is True and manifest.grain.startswith("1d/")
