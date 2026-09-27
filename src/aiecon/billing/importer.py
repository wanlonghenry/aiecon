"""Normalized CSV/JSON provider snapshot import (PLAN.md §7.4).

File contract: CSV columns
``record_id,window_start_ms,window_end_ms,dimensions_json,amount_original,amount_unit,currency,usage_json``
or a JSON document ``{"records": [...]}`` with the same field names (``dimensions_json`` and
``usage_json`` may be JSON strings or objects). A manifest JSON accompanies the file.

Rules: the manifest's ``source_hash`` must equal the SHA-256 of the file; re-importing a
snapshot id with the same bytes is a no-op and with different bytes a contract error, so
amounts are never added twice; the snapshot's ``data_kind`` must match the workspace; only
complete snapshots are registered. Every snapshot stays on file: which one supplies a given
UTC day is decided at read time (latest complete fetch covering that day), so a re-pull of
one day never hides the other days of an earlier pull (PLAN.md §7.4).
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aiecon.billing.units import UnsupportedAmountError, to_usd
from aiecon.privacy import safe_error_summary
from aiecon.spec.common import DataKind, Provider
from aiecon.spec.provider import ProviderRecord, ProviderSnapshotManifest, RecordKind
from aiecon.storage import Storage

CSV_COLUMNS = (
    "record_id",
    "window_start_ms",
    "window_end_ms",
    "dimensions_json",
    "amount_original",
    "amount_unit",
    "currency",
    "usage_json",
)


class BillingImportError(Exception):
    """Data-contract problem with a snapshot import (CLI exit code 3)."""


@dataclass
class ImportResult:
    snapshot_id: str
    record_count: int
    superseded_snapshot_ids: list[str] = field(default_factory=list)
    skipped_same_hash: bool = False
    non_usd_records: int = 0
    total_amount_usd: Decimal | None = None


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_manifest(path: Path) -> ProviderSnapshotManifest:
    try:
        return ProviderSnapshotManifest.model_validate_json(Path(path).read_text("utf-8"))
    except ValidationError as exc:
        raise BillingImportError(f"invalid manifest: {safe_error_summary(exc)}") from None


def _maybe_json(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return json.loads(value, parse_float=Decimal)
    return value


def read_records(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text("utf-8"), parse_float=Decimal)
        records = data.get("records") if isinstance(data, dict) else None
        if not isinstance(records, list):
            raise BillingImportError("JSON import must be an object with a 'records' array")
        return [dict(r) for r in records]
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing = [c for c in CSV_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise BillingImportError(f"CSV is missing columns: {', '.join(missing)}")
            return [dict(row) for row in reader]
    raise BillingImportError(f"unsupported import file type: {path.suffix}")


# ---------------------------------------------------------------- usage shapes
def normalize_usage(provider: Provider, native: dict[str, Any]) -> dict[str, int]:
    """Map provider-native usage counters to aiecon's mutually exclusive metrics."""

    def get(*path: str) -> int | None:
        node: Any = native
        for key in path:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node if isinstance(node, int) and not isinstance(node, bool) else None

    out: dict[str, int] = {}
    if provider is Provider.openai:
        total = get("input_tokens")
        read = get("input_cached_tokens")
        write = get("input_cache_write_tokens")
        uncached = get("input_uncached_tokens")
        if uncached is None and None not in (total, read, write):
            uncached = total - read - write  # type: ignore[operator]
        for key, value in (
            ("input_total_tokens", total),
            ("input_cache_read_tokens", read),
            ("input_cache_write_tokens", write),
            ("input_uncached_tokens", uncached),
            ("output_tokens", get("output_tokens")),
            ("requests", get("num_model_requests")),
        ):
            if value is not None:
                out[key] = value
    elif provider is Provider.anthropic:
        uncached = get("uncached_input_tokens")
        read = get("cache_read_input_tokens")
        write_5m = get("cache_creation", "ephemeral_5m_input_tokens")
        write_1h = get("cache_creation", "ephemeral_1h_input_tokens")
        write = None
        if write_5m is not None or write_1h is not None:
            write = (write_5m or 0) + (write_1h or 0)
        for key, value in (
            ("input_uncached_tokens", uncached),
            ("input_cache_read_tokens", read),
            ("input_cache_write_tokens", write),
            ("input_cache_write_5m_tokens", write_5m),
            ("input_cache_write_1h_tokens", write_1h),
            ("output_tokens", get("output_tokens")),
        ):
            if value is not None:
                out[key] = value
        if None not in (uncached, read, write):
            out["input_total_tokens"] = uncached + read + write  # type: ignore[operator]
    else:
        for key, value in native.items():
            if isinstance(value, int) and not isinstance(value, bool):
                out[key] = value
    return out


def _flatten_native(native: dict[str, Any]) -> dict[str, int]:
    flat: dict[str, int] = {}
    for key, value in native.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            flat[key] = value
        elif isinstance(value, dict):
            for sub, sub_value in value.items():
                if isinstance(sub_value, int) and not isinstance(sub_value, bool):
                    flat[f"{key}.{sub}"] = sub_value
    return flat


def _decimal(value: Any, field_name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise BillingImportError(f"{field_name} is not a decimal") from None


def build_records(
    manifest: ProviderSnapshotManifest, rows: list[dict[str, Any]], *, source_hash: str
) -> tuple[list[ProviderRecord], int]:
    records: list[ProviderRecord] = []
    non_usd = 0
    for index, row in enumerate(rows, start=1):
        try:
            dimensions = _maybe_json(row.get("dimensions_json")) or {}
            usage_native = _maybe_json(row.get("usage_json"))
        except (ValueError, TypeError):
            raise BillingImportError(
                f"row {index}: dimensions_json/usage_json is not JSON"
            ) from None
        if not isinstance(dimensions, dict):
            raise BillingImportError(f"row {index}: dimensions must be an object")
        amount_original = _decimal(row.get("amount_original"), f"row {index} amount_original")
        amount_unit = row.get("amount_unit") or None
        currency = row.get("currency") or None
        amount_usd: Decimal | None = None
        if manifest.record_kind is not RecordKind.provider_usage and amount_original is not None:
            try:
                amount_usd = to_usd(amount_original, amount_unit, currency)
            except UnsupportedAmountError:
                non_usd += 1
        usage = None
        native_flat = None
        if manifest.record_kind is RecordKind.provider_usage and isinstance(usage_native, dict):
            usage = normalize_usage(manifest.provider, usage_native)
            native_flat = _flatten_native(usage_native)
        elif isinstance(usage_native, dict):
            native_flat = _flatten_native(usage_native)
        try:
            records.append(
                ProviderRecord(
                    snapshot_id=manifest.snapshot_id,
                    record_id=str(row.get("record_id")),
                    provider=manifest.provider,
                    scope_id=manifest.scope_id,
                    record_kind=manifest.record_kind,
                    data_kind=manifest.data_kind,
                    window_start_ms=int(row["window_start_ms"]),
                    window_end_ms=int(row["window_end_ms"]),
                    grain=manifest.grain,
                    dimensions={
                        str(k): (None if v is None else str(v)) for k, v in dimensions.items()
                    },
                    amount_original=amount_original,
                    amount_unit=(amount_unit or "").lower() or None,
                    currency=currency.upper() if currency else None,
                    amount_usd=amount_usd,
                    usage=usage or None,
                    usage_native=native_flat or None,
                    source_ref=manifest.source_ref,
                    source_hash=source_hash,
                    fetched_at_ms=manifest.fetched_at_ms,
                    finality=manifest.finality,
                    snapshot_complete=manifest.snapshot_complete,
                )
            )
        except (ValidationError, KeyError, ValueError) as exc:
            detail = (
                safe_error_summary(exc) if isinstance(exc, ValidationError) else type(exc).__name__
            )
            raise BillingImportError(f"row {index}: {detail}") from None
    return records, non_usd


def import_snapshot(
    storage: Storage,
    *,
    file_path: Path,
    manifest_path: Path,
    now_ms: int,
    workspace_data_kind: DataKind | None,
) -> ImportResult:
    manifest = load_manifest(manifest_path)
    file_hash = sha256_file(file_path)
    if manifest.source_hash != file_hash:
        raise BillingImportError(
            "manifest source_hash does not match the import file; refusing to import data "
            "whose provenance cannot be verified"
        )
    if workspace_data_kind is not None and manifest.data_kind is not workspace_data_kind:
        raise BillingImportError(
            f"snapshot data_kind {manifest.data_kind.value} does not match the "
            f"{workspace_data_kind.value} workspace"
        )
    if manifest.data_kind is DataKind.live and len(manifest.source_ref.strip()) < 8:
        raise BillingImportError(
            "live snapshots need a real source_ref (API request or export origin)"
        )
    if not manifest.snapshot_complete:
        raise BillingImportError(
            "only complete snapshots may be activated; keep partial pulls in staging"
        )

    # Idempotency is per snapshot id: re-running the same import is a no-op, while the same
    # bytes under a new snapshot id (a later observation, e.g. provisional -> settled) are
    # registered as a new snapshot with their own manifest. Reusing an id for different
    # content is a contract error.
    existing_hash = storage.snapshot_source_hash(manifest.snapshot_id)
    if existing_hash is not None:
        if existing_hash == file_hash:
            return ImportResult(
                snapshot_id=manifest.snapshot_id, record_count=0, skipped_same_hash=True
            )
        raise BillingImportError(
            f"snapshot_id {manifest.snapshot_id} was already imported with different "
            "content; use a new snapshot_id for a new pull"
        )

    rows = read_records(file_path)
    records, non_usd = build_records(manifest, rows, source_hash=file_hash)
    with storage.transaction():
        count, superseded = storage.register_snapshot(manifest, records)
    total = None
    if manifest.record_kind is not RecordKind.provider_usage:
        total = sum((r.amount_usd for r in records if r.amount_usd is not None), start=Decimal(0))
    return ImportResult(
        snapshot_id=manifest.snapshot_id,
        record_count=count,
        superseded_snapshot_ids=superseded,
        non_usd_records=non_usd,
        total_amount_usd=total,
    )
