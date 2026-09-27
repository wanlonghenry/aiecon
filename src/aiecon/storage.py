"""Workspace layout, single-writer lock and DuckDB storage (PLAN.md §3.1, §3.2, §4.5).

Bulk writes and key lookups pass one JSON document per batch and unnest it inside DuckDB.
Binding thousands of individual parameters costs about 0.4 ms each in the Python client,
so a 300-row batch went from seconds to milliseconds with this shape.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel, ConfigDict

from aiecon.spec.call import ModelCall
from aiecon.spec.common import SCHEMA_VERSION, DataKind
from aiecon.spec.outcome import Outcome
from aiecon.spec.pricing import CostLineItem, PricingRunManifest
from aiecon.spec.provider import ProviderRecord, ProviderSnapshotManifest
from aiecon.spec.reconcile import ReconciliationBucket

DB_FILENAME = "aiecon.duckdb"
LOCK_FILENAME = "lock"
MANIFEST_FILENAME = "workspace.json"
BULK_ROWS = 5000


class WorkspaceError(Exception):
    """Configuration-level problem with the workspace (CLI exit code 2)."""


class WorkspaceLockedError(WorkspaceError):
    pass


class WorkspaceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    workspace_id: str
    data_kind: DataKind
    created_at_ms: int
    aiecon_version: str


class Workspace:
    """Directory layout for one dataset kind (demo or live)."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.raw_dir = self.root / "raw"
        self.provider_dir = self.root / "provider"
        self.reports_dir = self.root / "reports"
        self.state_dir = self.root / "state"
        self.db_path = self.root / DB_FILENAME
        self.manifest_path = self.state_dir / MANIFEST_FILENAME
        self.lock_path = self.state_dir / LOCK_FILENAME

    @property
    def exists(self) -> bool:
        return self.manifest_path.exists()

    def load_manifest(self) -> WorkspaceManifest:
        if not self.exists:
            raise WorkspaceError(
                f"workspace {self.root} is not initialised (run: aiecon --workspace PATH init)"
            )
        return WorkspaceManifest.model_validate_json(self.manifest_path.read_text("utf-8"))

    def init(
        self,
        *,
        data_kind: DataKind,
        now_ms: int,
        aiecon_version: str,
        workspace_id: str | None = None,
    ) -> WorkspaceManifest:
        for path in (self.root, self.raw_dir, self.provider_dir, self.reports_dir, self.state_dir):
            path.mkdir(parents=True, exist_ok=True)
        if self.exists:
            manifest = self.load_manifest()
            if manifest.data_kind is not data_kind:
                raise WorkspaceError(
                    f"workspace {self.root} holds {manifest.data_kind.value} data; "
                    f"refusing to reuse it for {data_kind.value}"
                )
            return manifest
        manifest = WorkspaceManifest(
            workspace_id=workspace_id or f"ws_{data_kind.value}_{now_ms}",
            data_kind=data_kind,
            created_at_ms=now_ms,
            aiecon_version=aiecon_version,
        )
        self.manifest_path.write_text(
            manifest.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        with Storage.open(self.db_path) as storage:
            storage.apply_schema()
            storage.set_meta("workspace_id", manifest.workspace_id)
            storage.set_meta("data_kind", data_kind.value)
            storage.set_meta("schema_version", SCHEMA_VERSION)
        return manifest

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Single writer per workspace. The lock file names the holder for a clear error."""

        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = ""
            try:
                holder = self.lock_path.read_text("utf-8").strip()
            except OSError:
                pass
            raise WorkspaceLockedError(
                f"workspace {self.root} is locked by another aiecon process ({holder}); "
                f"wait for it to finish or delete {self.lock_path} if that process is gone"
            ) from None
        try:
            os.write(fd, f"pid={os.getpid()}".encode())
            os.close(fd)
            yield
        finally:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass


def load_schema_sql() -> str:
    return resources.files("aiecon").joinpath("schema.sql").read_text("utf-8")


_STATEMENT_SPLIT = re.compile(r";\s*(?:\n|$)")


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ------------------------------------------------------------ table shapes
class _Table:
    """Column list plus the JSON struct types used to unnest a batch."""

    def __init__(
        self,
        name: str,
        columns: Sequence[str],
        *,
        bigint: Sequence[str] = (),
        boolean: Sequence[str] = (),
        decimal: Sequence[str] = (),
    ):
        self.name = name
        self.columns = list(columns)
        self.column_sql = ", ".join(self.columns)
        types = {c: "VARCHAR" for c in self.columns}
        types.update({c: "BIGINT" for c in bigint})
        types.update({c: "BOOLEAN" for c in boolean})
        self.struct_sql = ", ".join(f'"{c}": "{types[c]}"' for c in self.columns)
        self.select_sql = ", ".join(
            f"CAST({c} AS DECIMAL(24,12))" if c in decimal else c for c in self.columns
        )

    def insert_sql(self, *, replace: bool) -> str:
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        return (
            f"{verb} INTO {self.name} ({self.column_sql}) SELECT {self.select_sql} "
            f"FROM (SELECT unnest(from_json(?::JSON, '[{{{self.struct_sql}}}]'), "
            f"recursive := true))"
        )


CALLS = _Table(
    "calls",
    [
        "dataset_id",
        "call_id",
        "data_kind",
        "scope_id",
        "workflow_id",
        "workflow_run_id",
        "node_id",
        "node_run_id",
        "attempt_index",
        "retry_of_call_id",
        "fallback_of_call_id",
        "provider",
        "api_family",
        "model_requested",
        "model_resolved",
        "service_tier",
        "inference_region",
        "provider_request_id",
        "started_at_ms",
        "ended_at_ms",
        "status",
        "error_class",
        "usage_format",
        "input_total_tokens",
        "input_uncached_tokens",
        "input_cache_read_tokens",
        "input_cache_write_tokens",
        "output_tokens",
        "usage_completeness",
        "upstream_cost_estimate_usd",
        "prefix_fingerprint",
        "fingerprint_key_id",
        "prefix_tokens",
        "cache_policy",
        "stream",
        "revision",
        "normalizer_version",
        "doc_json",
    ],
    bigint=[
        "attempt_index",
        "started_at_ms",
        "ended_at_ms",
        "input_total_tokens",
        "input_uncached_tokens",
        "input_cache_read_tokens",
        "input_cache_write_tokens",
        "output_tokens",
        "prefix_tokens",
        "revision",
    ],
    boolean=["stream"],
    decimal=["upstream_cost_estimate_usd"],
)
CALL_COLUMNS = CALLS.column_sql


def _call_row(call: ModelCall) -> list[Any]:
    return [
        call.dataset_id,
        call.call_id,
        call.data_kind.value,
        call.scope_id,
        call.workflow_id,
        call.workflow_run_id,
        call.node_id,
        call.node_run_id,
        call.attempt_index,
        call.retry_of_call_id,
        call.fallback_of_call_id,
        call.provider.value,
        call.api_family.value,
        call.model_requested,
        call.model_resolved,
        call.service_tier,
        call.inference_region,
        call.provider_request_id,
        call.started_at_ms,
        call.ended_at_ms,
        call.status.value,
        call.error_class,
        call.usage_format.value if call.usage_format else None,
        call.input_total_tokens,
        call.input_uncached_tokens,
        call.input_cache_read_tokens,
        call.input_cache_write_tokens,
        call.output_tokens,
        call.usage_completeness.value,
        _dec(call.upstream_cost_estimate_usd),
        call.prefix_fingerprint,
        call.fingerprint_key_id,
        call.prefix_tokens,
        call.cache_policy,
        call.stream,
        call.revision,
        call.normalizer_version,
        call.model_dump_json(),
    ]


OUTCOMES = _Table(
    "outcomes",
    [
        "dataset_id",
        "workflow_run_id",
        "workflow_id",
        "data_kind",
        "revision",
        "terminal_at_ms",
        "status",
        "success",
        "outcome_source",
        "doc_json",
    ],
    bigint=["revision", "terminal_at_ms"],
    boolean=["success"],
)


def _outcome_row(outcome: Outcome) -> list[Any]:
    return [
        outcome.dataset_id,
        outcome.workflow_run_id,
        outcome.workflow_id,
        outcome.data_kind.value,
        outcome.revision,
        outcome.terminal_at_ms,
        outcome.status.value,
        outcome.success,
        outcome.outcome_source.value,
        outcome.model_dump_json(),
    ]


EVENTS = _Table(
    "meta_events",
    [
        "dataset_id",
        "event_id",
        "content_hash",
        "event_type",
        "revision",
        "target_id",
        "source_file",
        "source_line",
        "processed_at_ms",
        "applied",
    ],
    bigint=["revision", "source_line", "processed_at_ms"],
    boolean=["applied"],
)

LINE_ITEMS = _Table(
    "cost_line_items",
    [
        "dataset_id",
        "call_id",
        "resource",
        "pricing_run_id",
        "line_item_id",
        "data_kind",
        "quantity",
        "unit",
        "unit_quantity",
        "unit_price",
        "currency",
        "line_cost",
        "status",
        "unpriced_reason",
        "evidence_class",
        "catalog_version",
        "price_id",
        "boundary_call",
        "doc_json",
    ],
    bigint=["quantity", "unit_quantity"],
    boolean=["boundary_call"],
    decimal=["unit_price", "line_cost"],
)


def _line_item_row(item: CostLineItem) -> list[Any]:
    return [
        item.dataset_id,
        item.call_id,
        item.resource.value,
        item.pricing_run_id,
        item.line_item_id,
        item.data_kind.value,
        item.quantity,
        item.unit.value,
        item.unit_quantity,
        _dec(item.unit_price),
        item.currency,
        _dec(item.line_cost),
        item.status.value,
        item.unpriced_reason,
        item.evidence_class.value,
        item.catalog_version,
        item.price_id,
        item.boundary_call,
        item.model_dump_json(),
    ]


RECORDS = _Table(
    "provider_records",
    [
        "snapshot_id",
        "record_id",
        "provider",
        "scope_id",
        "record_kind",
        "data_kind",
        "window_start_ms",
        "window_end_ms",
        "grain",
        "dimensions_json",
        "dim_model",
        "dim_project",
        "dim_line_item",
        "amount_original",
        "amount_unit",
        "currency",
        "amount_usd",
        "usage_json",
        "source_ref",
        "source_hash",
        "fetched_at_ms",
        "finality",
        "snapshot_complete",
        "doc_json",
    ],
    bigint=["window_start_ms", "window_end_ms", "fetched_at_ms"],
    boolean=["snapshot_complete"],
    decimal=["amount_original", "amount_usd"],
)


def _record_row(record: ProviderRecord) -> list[Any]:
    dims = record.dimensions
    return [
        record.snapshot_id,
        record.record_id,
        record.provider.value,
        record.scope_id,
        record.record_kind.value,
        record.data_kind.value,
        record.window_start_ms,
        record.window_end_ms,
        record.grain,
        json.dumps(dims, sort_keys=True),
        dims.get("model"),
        dims.get("project_id", dims.get("workspace_id")),
        dims.get("line_item", dims.get("description")),
        _dec(record.amount_original),
        record.amount_unit,
        record.currency,
        _dec(record.amount_usd),
        None if record.usage is None else json.dumps(record.usage, sort_keys=True),
        record.source_ref,
        record.source_hash,
        record.fetched_at_ms,
        record.finality.value,
        record.snapshot_complete,
        record.model_dump_json(),
    ]


BUCKETS = _Table(
    "reconciliation_buckets",
    [
        "reconcile_run_id",
        "bucket_key",
        "comparison_kind",
        "dataset_id",
        "provider",
        "scope_id",
        "window_start_ms",
        "window_end_ms",
        "grain",
        "dimensions_json",
        "local_estimate_usd",
        "provider_cost_usd",
        "signed_variance_usd",
        "variance_pct",
        "status",
        "doc_json",
    ],
    bigint=["window_start_ms", "window_end_ms"],
    decimal=["local_estimate_usd", "provider_cost_usd", "signed_variance_usd", "variance_pct"],
)


def _bucket_row(bucket: ReconciliationBucket) -> list[Any]:
    return [
        bucket.reconcile_run_id,
        bucket.bucket_key,
        bucket.comparison_kind.value,
        bucket.dataset_id,
        bucket.provider.value,
        bucket.scope_id,
        bucket.window_start_ms,
        bucket.window_end_ms,
        bucket.grain,
        json.dumps(bucket.dimensions, sort_keys=True),
        _dec(bucket.local_estimate_usd),
        _dec(bucket.provider_cost_usd),
        _dec(bucket.signed_variance_usd),
        _dec(bucket.variance_pct),
        bucket.status.value,
        bucket.model_dump_json(),
    ]


def _ids_json(ids: Iterable[str]) -> str:
    return json.dumps(list(ids))


ID_LIST = "(SELECT unnest(from_json(?::JSON, '[\"VARCHAR\"]')))"


class Storage:
    """Thin DuckDB wrapper. All writes happen inside :meth:`transaction`."""

    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con

    @classmethod
    def open(cls, db_path: Path | str, *, read_only: bool = False) -> Storage:
        return cls(duckdb.connect(str(db_path), read_only=read_only))

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.con.close()

    # ------------------------------------------------------------------ schema
    def apply_schema(self) -> None:
        for statement in _STATEMENT_SPLIT.split(load_schema_sql()):
            text = statement.strip()
            if not text or all(line.strip().startswith("--") for line in text.splitlines()):
                continue
            self.con.execute(text)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.con.execute("BEGIN TRANSACTION")
        try:
            yield
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        self.con.execute("COMMIT")

    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        return self.con.execute(sql, list(params or [])).fetchall()

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = self.con.execute(sql, list(params or [])).fetchone()
        return None if row is None else row[0]

    def _bulk(self, table: _Table, rows: Sequence[Sequence[Any]], *, replace: bool) -> int:
        sql = table.insert_sql(replace=replace)
        count = 0
        for chunk in _chunks(rows, BULK_ROWS):
            payload = json.dumps([dict(zip(table.columns, row, strict=True)) for row in chunk])
            self.con.execute(sql, [payload])
            count += len(chunk)
        return count

    # -------------------------------------------------------------------- meta
    def set_meta(self, key: str, value: str) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO meta_workspace (key, value) VALUES (?, ?)", [key, value]
        )

    def get_meta(self, key: str) -> str | None:
        return self.scalar("SELECT value FROM meta_workspace WHERE key = ?", [key])

    def get_event(self, dataset_id: str, event_id: str) -> tuple[str, bool] | None:
        row = self.con.execute(
            "SELECT content_hash, applied FROM meta_events WHERE dataset_id = ? AND event_id = ?",
            [dataset_id, event_id],
        ).fetchone()
        return None if row is None else (row[0], bool(row[1]))

    def load_event_hashes(self, keys: Sequence[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """Content hashes for the given (dataset_id, event_id) pairs that already exist."""

        found: dict[tuple[str, str], str] = {}
        by_dataset: dict[str, list[str]] = {}
        for dataset_id, event_id in keys:
            by_dataset.setdefault(dataset_id, []).append(event_id)
        for dataset_id, event_ids in by_dataset.items():
            rows = self.query(
                "SELECT event_id, content_hash FROM meta_events WHERE dataset_id = ? "
                f"AND event_id IN {ID_LIST}",
                [dataset_id, _ids_json(event_ids)],
            )
            for event_id, content_hash in rows:
                found[(dataset_id, event_id)] = content_hash
        return found

    def record_events(self, rows: Sequence[Sequence[Any]]) -> int:
        """Rows follow the meta_events column order (see EVENTS)."""

        return self._bulk(EVENTS, rows, replace=False)

    def record_event(
        self,
        *,
        dataset_id: str,
        event_id: str,
        content_hash: str,
        event_type: str,
        revision: int,
        target_id: str,
        source_file: str | None,
        source_line: int | None,
        processed_at_ms: int,
        applied: bool,
    ) -> None:
        self.record_events(
            [
                [
                    dataset_id,
                    event_id,
                    content_hash,
                    event_type,
                    revision,
                    target_id,
                    source_file,
                    source_line,
                    processed_at_ms,
                    applied,
                ]
            ]
        )

    def file_already_ingested(self, file_path: str, file_sha256: str) -> bool:
        return (
            self.scalar(
                "SELECT COUNT(*) FROM meta_ingest_files WHERE file_path = ? AND file_sha256 = ?",
                [file_path, file_sha256],
            )
            > 0
        )

    def record_ingest_file(
        self,
        *,
        file_path: str,
        file_sha256: str,
        dataset_id: str | None,
        line_count: int,
        accepted: int,
        duplicates: int,
        conflicts: int,
        rejected: int,
        truncated_tail: bool,
        processed_at_ms: int,
    ) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO meta_ingest_files (file_path, file_sha256, dataset_id, "
            "line_count, accepted, duplicates, conflicts, rejected, truncated_tail, "
            "processed_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                file_path,
                file_sha256,
                dataset_id,
                line_count,
                accepted,
                duplicates,
                conflicts,
                rejected,
                truncated_tail,
                processed_at_ms,
            ],
        )

    # ------------------------------------------------------------------- calls
    def upsert_calls(self, calls: Iterable[ModelCall]) -> int:
        return self._bulk(CALLS, [_call_row(call) for call in calls], replace=True)

    def upsert_call(self, call: ModelCall) -> None:
        self.upsert_calls([call])

    def get_call(self, dataset_id: str, call_id: str) -> ModelCall | None:
        doc = self.scalar(
            "SELECT doc_json FROM calls WHERE dataset_id = ? AND call_id = ?", [dataset_id, call_id]
        )
        return None if doc is None else ModelCall.model_validate_json(doc)

    def get_calls_many(self, keys: Sequence[tuple[str, str]]) -> dict[tuple[str, str], ModelCall]:
        found: dict[tuple[str, str], ModelCall] = {}
        by_dataset: dict[str, list[str]] = {}
        for dataset_id, call_id in keys:
            by_dataset.setdefault(dataset_id, []).append(call_id)
        for dataset_id, call_ids in by_dataset.items():
            rows = self.query(
                "SELECT call_id, doc_json FROM calls WHERE dataset_id = ? "
                f"AND call_id IN {ID_LIST}",
                [dataset_id, _ids_json(call_ids)],
            )
            for call_id, doc in rows:
                found[(dataset_id, call_id)] = ModelCall.model_validate_json(doc)
        return found

    def list_calls(self, dataset_id: str) -> list[ModelCall]:
        rows = self.query(
            "SELECT doc_json FROM calls WHERE dataset_id = ? ORDER BY started_at_ms, call_id",
            [dataset_id],
        )
        return [ModelCall.model_validate_json(row[0]) for row in rows]

    def count_calls(self, dataset_id: str) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM calls WHERE dataset_id = ?", [dataset_id]))

    # ---------------------------------------------------------------- outcomes
    def upsert_outcomes(self, outcomes: Iterable[Outcome]) -> int:
        return self._bulk(OUTCOMES, [_outcome_row(o) for o in outcomes], replace=True)

    def upsert_outcome(self, outcome: Outcome) -> None:
        self.upsert_outcomes([outcome])

    def get_outcome(self, dataset_id: str, workflow_run_id: str) -> Outcome | None:
        doc = self.scalar(
            "SELECT doc_json FROM outcomes WHERE dataset_id = ? AND workflow_run_id = ?",
            [dataset_id, workflow_run_id],
        )
        return None if doc is None else Outcome.model_validate_json(doc)

    def get_outcomes_many(self, keys: Sequence[tuple[str, str]]) -> dict[tuple[str, str], Outcome]:
        found: dict[tuple[str, str], Outcome] = {}
        by_dataset: dict[str, list[str]] = {}
        for dataset_id, run_id in keys:
            by_dataset.setdefault(dataset_id, []).append(run_id)
        for dataset_id, run_ids in by_dataset.items():
            rows = self.query(
                "SELECT workflow_run_id, doc_json FROM outcomes WHERE dataset_id = ? "
                f"AND workflow_run_id IN {ID_LIST}",
                [dataset_id, _ids_json(run_ids)],
            )
            for run_id, doc in rows:
                found[(dataset_id, run_id)] = Outcome.model_validate_json(doc)
        return found

    def list_outcomes(self, dataset_id: str) -> list[Outcome]:
        rows = self.query(
            "SELECT doc_json FROM outcomes WHERE dataset_id = ? ORDER BY workflow_run_id",
            [dataset_id],
        )
        return [Outcome.model_validate_json(row[0]) for row in rows]

    def count_outcomes(self, dataset_id: str) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM outcomes WHERE dataset_id = ?", [dataset_id]))

    def list_dataset_ids(self) -> list[str]:
        rows = self.query(
            "SELECT DISTINCT dataset_id FROM calls UNION SELECT DISTINCT dataset_id FROM outcomes "
            "ORDER BY 1"
        )
        return [row[0] for row in rows]

    # -------------------------------------------------------------- line items
    def replace_line_items(self, pricing_run_id: str, items: Iterable[CostLineItem]) -> int:
        self.con.execute("DELETE FROM cost_line_items WHERE pricing_run_id = ?", [pricing_run_id])
        return self._bulk(LINE_ITEMS, [_line_item_row(i) for i in items], replace=False)

    def list_line_items(self, pricing_run_id: str) -> list[CostLineItem]:
        rows = self.query(
            "SELECT doc_json FROM cost_line_items WHERE pricing_run_id = ? "
            "ORDER BY call_id, resource",
            [pricing_run_id],
        )
        return [CostLineItem.model_validate_json(row[0]) for row in rows]

    # -------------------------------------------------------------------- runs
    def register_run(
        self,
        *,
        run_id: str,
        run_kind: str,
        dataset_id: str,
        created_at_ms: int,
        manifest_json: str,
        activate: bool,
    ) -> None:
        if activate:
            self.con.execute(
                "UPDATE meta_runs SET active = FALSE WHERE run_kind = ? AND dataset_id = ?",
                [run_kind, dataset_id],
            )
        self.con.execute(
            "INSERT OR REPLACE INTO meta_runs (run_id, run_kind, dataset_id, created_at_ms, "
            "active, manifest_json) VALUES (?, ?, ?, ?, ?, ?)",
            [run_id, run_kind, dataset_id, created_at_ms, activate, manifest_json],
        )

    def active_run(self, run_kind: str, dataset_id: str) -> tuple[str, str] | None:
        row = self.con.execute(
            "SELECT run_id, manifest_json FROM meta_runs WHERE run_kind = ? AND dataset_id = ? "
            "AND active ORDER BY created_at_ms DESC LIMIT 1",
            [run_kind, dataset_id],
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def active_pricing_run(self, dataset_id: str) -> PricingRunManifest | None:
        found = self.active_run("pricing", dataset_id)
        return None if found is None else PricingRunManifest.model_validate_json(found[1])

    # --------------------------------------------------------------- snapshots
    def register_snapshot(
        self, manifest: ProviderSnapshotManifest, records: Iterable[ProviderRecord]
    ) -> tuple[int, list[str]]:
        """Insert a complete snapshot and make it the only active one for its key.

        Returns ``(record_count, deactivated_snapshot_ids)``. Snapshots sharing provider,
        scope, record_kind, grain and an overlapping window are replaced as a whole, so rows
        that disappeared upstream disappear here too (PLAN.md §7.4).
        """

        rows = self.query(
            "SELECT snapshot_id FROM meta_snapshots WHERE provider = ? AND scope_id = ? AND "
            "record_kind = ? AND grain = ? AND data_kind = ? AND active AND "
            "window_start_ms < ? AND window_end_ms > ? AND snapshot_id <> ?",
            [
                manifest.provider.value,
                manifest.scope_id,
                manifest.record_kind.value,
                manifest.grain,
                manifest.data_kind.value,
                manifest.query_window.end_ms,
                manifest.query_window.start_ms,
                manifest.snapshot_id,
            ],
        )
        deactivated = [row[0] for row in rows]
        for snapshot_id in deactivated:
            self.con.execute(
                "UPDATE meta_snapshots SET active = FALSE WHERE snapshot_id = ?", [snapshot_id]
            )
        self.con.execute(
            "DELETE FROM provider_records WHERE snapshot_id = ?", [manifest.snapshot_id]
        )
        count = self._bulk(RECORDS, [_record_row(r) for r in records], replace=False)
        self.con.execute(
            "INSERT OR REPLACE INTO meta_snapshots (snapshot_id, provider, scope_id, record_kind, "
            "data_kind, grain, window_start_ms, window_end_ms, fetched_at_ms, source_hash, "
            "finality, snapshot_complete, active, record_count, manifest_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?, ?)",
            [
                manifest.snapshot_id,
                manifest.provider.value,
                manifest.scope_id,
                manifest.record_kind.value,
                manifest.data_kind.value,
                manifest.grain,
                manifest.query_window.start_ms,
                manifest.query_window.end_ms,
                manifest.fetched_at_ms,
                manifest.source_hash,
                manifest.finality.value,
                manifest.snapshot_complete,
                count,
                manifest.model_dump_json(),
            ],
        )
        return count, deactivated

    def snapshot_exists_with_hash(self, source_hash: str) -> str | None:
        return self.scalar(
            "SELECT snapshot_id FROM meta_snapshots WHERE source_hash = ? AND active LIMIT 1",
            [source_hash],
        )

    def list_snapshots(self, *, active_only: bool = True) -> list[ProviderSnapshotManifest]:
        sql = "SELECT manifest_json FROM meta_snapshots"
        if active_only:
            sql += " WHERE active"
        sql += " ORDER BY fetched_at_ms, snapshot_id"
        return [ProviderSnapshotManifest.model_validate_json(row[0]) for row in self.query(sql)]

    def list_provider_records(self, *, active_only: bool = True) -> list[ProviderRecord]:
        table = "current_provider_records" if active_only else "provider_records"
        rows = self.query(f"SELECT doc_json FROM {table} ORDER BY window_start_ms, record_id")
        return [ProviderRecord.model_validate_json(row[0]) for row in rows]

    # ----------------------------------------------------------------- buckets
    def replace_buckets(
        self, reconcile_run_id: str, buckets: Iterable[ReconciliationBucket]
    ) -> int:
        self.con.execute(
            "DELETE FROM reconciliation_buckets WHERE reconcile_run_id = ?", [reconcile_run_id]
        )
        return self._bulk(BUCKETS, [_bucket_row(b) for b in buckets], replace=False)

    def list_buckets(self, reconcile_run_id: str) -> list[ReconciliationBucket]:
        rows = self.query(
            "SELECT doc_json FROM reconciliation_buckets WHERE reconcile_run_id = ? "
            "ORDER BY comparison_kind, bucket_key",
            [reconcile_run_id],
        )
        return [ReconciliationBucket.model_validate_json(row[0]) for row in rows]
