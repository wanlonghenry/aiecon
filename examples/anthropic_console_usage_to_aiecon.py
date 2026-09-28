#!/usr/bin/env python
"""Convert an Anthropic Console *token usage* CSV export into an aiecon provider snapshot.

The Console export (``claude_api_tokens_<from>_to_<to>.csv``) has one row per UTC day,
model, API key, workspace, usage type, context window, inference geo and speed with the
columns ``usage_input_tokens_no_cache``, ``usage_input_tokens_cache_write_5m``,
``usage_input_tokens_cache_write_1h``, ``usage_input_tokens_cache_read``,
``usage_output_tokens`` and ``web_search_count``. This script maps every row to the shape of
the Admin usage report that ``aiecon billing import`` already understands (PLAN.md 7.4) and
writes ``records.json`` plus ``manifest.json`` into an output folder:

    uv run python examples/anthropic_console_usage_to_aiecon.py \\
        --csv ~/Downloads/claude_api_tokens_2026_08_30_to_2026_09_28.csv \\
        --scope-id live_anthropic_workspace --out .aiecon/live/provider/imports/anthropic_usage \\
        --snapshot-id snap_anthropic_usage_console_20260928 --dedicated-scope
    uv run aiecon --workspace .aiecon/live billing import \\
        --file .aiecon/live/provider/imports/anthropic_usage/records.json \\
        --manifest .aiecon/live/provider/imports/anthropic_usage/manifest.json

Token usage is not money: the result is a ``provider_usage`` snapshot. Amounts come from
the Console *cost* export or the Admin cost report, never from these counts.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiecon.config import now_ms
from aiecon.spec.provider import ProviderSnapshotManifest, grain_key

DAY_MS = 86_400_000
REQUIRED = (
    "usage_date_utc",
    "model_version",
    "usage_input_tokens_no_cache",
    "usage_input_tokens_cache_write_5m",
    "usage_input_tokens_cache_write_1h",
    "usage_input_tokens_cache_read",
    "usage_output_tokens",
)
DIMENSION_COLUMNS = {
    "model_version": "model",
    "workspace": "workspace_id",
    "api_key": "api_key_id",
    "usage_type": "service_tier",
    "context_window": "context_window",
    "inference_geo": "inference_geo",
    "speed": "speed",
}
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str) -> str:
    return _SLUG.sub("_", value.strip()).strip("_") or "none"


def _int(row: dict[str, str], column: str) -> int:
    text = (row.get(column) or "").strip().replace(",", "")
    return int(text) if text else 0


def _day_ms(text: str) -> int:
    day = datetime.strptime(text.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    return int(day.timestamp() * 1000)


def convert_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows of the Console CSV -> aiecon import records and the dimension names used."""

    records: list[dict[str, Any]] = []
    dims_used: set[str] = set()
    for row in rows:
        day = _day_ms(row["usage_date_utc"])
        dimensions: dict[str, str] = {}
        for column, name in DIMENSION_COLUMNS.items():
            value = (row.get(column) or "").strip()
            if not value or value == "not_available":
                continue
            dimensions[name] = value.replace("≤", "<=")
            dims_used.add(name)
        usage: dict[str, Any] = {
            "uncached_input_tokens": _int(row, "usage_input_tokens_no_cache"),
            "cache_read_input_tokens": _int(row, "usage_input_tokens_cache_read"),
            "cache_creation": {
                "ephemeral_5m_input_tokens": _int(row, "usage_input_tokens_cache_write_5m"),
                "ephemeral_1h_input_tokens": _int(row, "usage_input_tokens_cache_write_1h"),
            },
            "output_tokens": _int(row, "usage_output_tokens"),
        }
        searches = _int(row, "web_search_count")
        if searches:
            usage["server_tool_use"] = {"web_search_requests": searches}
        record_id = "_".join(
            [row["usage_date_utc"].strip()]
            + [_slug(dimensions.get(name, "none")) for name in DIMENSION_COLUMNS.values()]
        )
        records.append(
            {
                "record_id": record_id,
                "window_start_ms": day,
                "window_end_ms": day + DAY_MS,
                "dimensions_json": dimensions,
                "amount_original": None,
                "amount_unit": None,
                "currency": None,
                "usage_json": usage,
            }
        )
    return records, sorted(dims_used)


def read_csv(path: Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"not an Anthropic Console usage export; missing {missing}")
        return [row for row in reader if (row.get("usage_date_utc") or "").strip()]


def write_snapshot(
    records: list[dict[str, Any]],
    dims: list[str],
    *,
    out_dir: Path,
    snapshot_id: str,
    scope_id: str,
    source_ref: str,
    query_window: tuple[int, int],
    fetched_at_ms: int,
    dedicated: bool | None,
    data_kind: str = "live",
) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"records": records}, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    records_path = out_dir / "records.json"
    records_path.write_text(body, encoding="utf-8", newline="\n")
    manifest = ProviderSnapshotManifest(
        snapshot_id=snapshot_id,
        provider="anthropic",
        scope_id=scope_id,
        record_kind="provider_usage",
        data_kind=data_kind,
        grain=grain_key("1d", dims),
        query_window={"start_ms": query_window[0], "end_ms": query_window[1]},
        fetched_at_ms=fetched_at_ms,
        source_ref=source_ref,
        source_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        finality="provisional",
        snapshot_complete=True,
        record_count=len(records),
        source_type="file_import",
        scope_dedicated=dedicated,
        notes=(
            "Token counts from the Anthropic Console usage export, mapped to the Admin usage "
            "report shape; not money and not an invoice."
        ),
    )
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(json.loads(manifest.model_dump_json()), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return records_path, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scope-id", required=True, help="scope id the local calls carry")
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--start", help="UTC date the export covers (default: first row)")
    parser.add_argument("--end", help="UTC date after the last covered day (default: last row + 1)")
    parser.add_argument(
        "--dedicated-scope",
        action="store_true",
        help="the exported workspace/key carries only the traffic aiecon captured",
    )
    parser.add_argument("--data-kind", default="live", choices=("live", "synthetic"))
    args = parser.parse_args()

    rows = read_csv(args.csv)
    if not rows:
        print("the export has no usage rows", file=sys.stderr)
        return 2
    records, dims = convert_rows(rows)
    days = sorted(r["window_start_ms"] for r in records)
    start = _day_ms(args.start) if args.start else days[0]
    end = _day_ms(args.end) if args.end else days[-1] + DAY_MS
    if end <= start:
        print("--end must be after --start", file=sys.stderr)
        return 2
    records_path, manifest_path = write_snapshot(
        records,
        dims,
        out_dir=args.out,
        snapshot_id=args.snapshot_id,
        scope_id=args.scope_id,
        source_ref=f"Anthropic Console usage export {Path(args.csv).name}",
        query_window=(start, end),
        fetched_at_ms=now_ms(),
        dedicated=True if args.dedicated_scope else None,
        data_kind=args.data_kind,
    )
    print(f"records  {records_path} ({len(records)} rows, dimensions {', '.join(dims)})")
    print(f"manifest {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
