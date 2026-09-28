#!/usr/bin/env python
"""Convert an OpenAI dashboard *cost* CSV export into an aiecon provider snapshot.

The dashboard export (``cost_<from>_<to>.csv``) has one row per time bucket and grouping
with the columns ``start_time,end_time,start_time_iso,end_time_iso,amount_value,
amount_currency,quantity,quantity_unit,line_item,user_id,api_key_id,project_id,
organization_id,project_name,organization_name,user_email,api_source``. Buckets without an
amount are empty days and are skipped. Only ids and the line item become dimensions; names
and e-mail addresses never leave the file. The result is a ``provider_cost`` snapshot for
``aiecon billing import`` (PLAN.md 7.4):

    uv run python examples/openai_dashboard_cost_to_aiecon.py \\
        --csv ~/Downloads/cost_2026-09-25_2026-09-29.csv \\
        --scope-id live_openai_project --out .aiecon/live/provider/imports/openai_cost \\
        --snapshot-id snap_openai_cost_dashboard_20260928 --dedicated-scope

Without a ``project_id`` column value the export is an organisation-wide total; declare the
scope dedicated only when nothing but the captured traffic ran in that organisation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from aiecon.config import now_ms
from aiecon.spec.provider import NOT_GROUPED, ProviderSnapshotManifest, grain_key

REQUIRED = ("start_time", "end_time", "amount_value", "amount_currency")
ID_COLUMNS = {
    "project_id": "project_id",
    "api_key_id": "api_key_id",
    "organization_id": "organization_id",
    "user_id": "user_id",
}
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str) -> str:
    return _SLUG.sub("_", value.strip()).strip("_") or "none"


def _decimal(text: str | None) -> Decimal | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def convert_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[str], int]:
    """Rows of the dashboard CSV -> aiecon cost records, dimension names, skipped buckets."""

    records: list[dict[str, Any]] = []
    dims_used: set[str] = set()
    skipped = 0
    for row in rows:
        amount = _decimal(row.get("amount_value"))
        if amount is None:
            skipped += 1  # an empty bucket: no charge in that window
            continue
        start = int(row["start_time"].strip()) * 1000
        end = int(row["end_time"].strip()) * 1000
        dimensions: dict[str, str] = {}
        for column, name in ID_COLUMNS.items():
            value = (row.get(column) or "").strip()
            if value:
                dimensions[name] = value
                dims_used.add(name)
        line_item = (row.get("line_item") or "").strip()
        dimensions["line_item"] = line_item or NOT_GROUPED
        if line_item:
            dims_used.add("line_item")
        if "project_id" not in dimensions:
            dimensions["project_id"] = NOT_GROUPED  # organisation-wide bucket
        record_id = "_".join(
            [
                datetime.fromtimestamp(start // 1000, tz=UTC).strftime("%Y-%m-%d"),
                _slug(dimensions.get("project_id", NOT_GROUPED)),
                _slug(dimensions.get("api_key_id", "none")),
                _slug(dimensions["line_item"]),
            ]
        )
        quantity = _decimal(row.get("quantity"))
        usage_native: dict[str, Any] | None = None
        if quantity is not None and quantity == quantity.to_integral_value():
            usage_native = {f"quantity_{_slug(row.get('quantity_unit') or 'units')}": int(quantity)}
        records.append(
            {
                "record_id": record_id,
                "window_start_ms": start,
                "window_end_ms": end,
                "dimensions_json": dimensions,
                "amount_original": format(amount, "f"),
                "amount_unit": "usd",
                "currency": (row.get("amount_currency") or "usd").strip().upper(),
                "usage_json": usage_native,
            }
        )
    return records, sorted(dims_used or {"project_id"}), skipped


def read_csv(path: Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(
                f"not an OpenAI dashboard cost export with amounts; missing {missing} "
                "(export the Cost view with amount columns)"
            )
        return [row for row in reader if (row.get("start_time") or "").strip()]


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
    body = json.dumps({"records": records}, indent=2, sort_keys=True) + "\n"
    records_path = out_dir / "records.json"
    records_path.write_text(body, encoding="utf-8", newline="\n")
    manifest = ProviderSnapshotManifest(
        snapshot_id=snapshot_id,
        provider="openai",
        scope_id=scope_id,
        record_kind="provider_cost",
        data_kind=data_kind,
        grain=grain_key("1d", dims),
        query_window={"start_ms": query_window[0], "end_ms": query_window[1]},
        fetched_at_ms=fetched_at_ms,
        source_ref=source_ref,
        source_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        finality="provisional",
        snapshot_complete=True,
        record_count=len(records),
        currency="USD",
        source_type="file_import",
        scope_dedicated=dedicated,
        notes=(
            "Amounts from the OpenAI dashboard cost export (provider-reported, provisional); "
            "not an invoice. Empty buckets in the export are days without charges."
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
    parser.add_argument(
        "--dedicated-scope",
        action="store_true",
        help="the exported project/organisation carries only the traffic aiecon captured",
    )
    parser.add_argument("--data-kind", default="live", choices=("live", "synthetic"))
    args = parser.parse_args()

    rows = read_csv(args.csv)
    if not rows:
        print("the export has no buckets", file=sys.stderr)
        return 2
    records, dims, skipped = convert_rows(rows)
    starts = [int(r["start_time"]) * 1000 for r in rows]
    ends = [int(r["end_time"]) * 1000 for r in rows]
    records_path, manifest_path = write_snapshot(
        records,
        dims,
        out_dir=args.out,
        snapshot_id=args.snapshot_id,
        scope_id=args.scope_id,
        source_ref=f"OpenAI dashboard cost export {Path(args.csv).name}",
        query_window=(min(starts), max(ends)),
        fetched_at_ms=now_ms(),
        dedicated=True if args.dedicated_scope else None,
        data_kind=args.data_kind,
    )
    total = sum((Decimal(r["amount_original"]) for r in records), start=Decimal(0))
    print(
        f"records  {records_path} ({len(records)} charged buckets, {skipped} empty; "
        f"dimensions {', '.join(dims)}; total {total:f} USD)"
    )
    print(f"manifest {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
