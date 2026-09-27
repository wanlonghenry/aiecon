"""``billing sync``: fetch complete snapshots, stage them as files, then import (§7.4).

The poller never writes a partial snapshot: every page is fetched first, the records and a
manifest are written under ``<workspace>/provider/sync/<snapshot_id>/`` and only then does
the ordinary file importer register the snapshot. A failed pull leaves earlier snapshots
untouched (V16). Every pull is kept; which snapshot supplies a given UTC day is decided at
read time (latest complete fetch covering that day, see ``aiecon.reconcile``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from aiecon.billing.anthropic import AnthropicReportSource
from aiecon.billing.importer import ImportResult, import_snapshot
from aiecon.billing.openai import OpenAIReportSource, dumps_records
from aiecon.config import DEFAULT_SETTINGS
from aiecon.spec.common import DataKind, Provider, TimeWindow
from aiecon.spec.provider import Finality, ProviderSnapshotManifest, RecordKind
from aiecon.storage import Storage, Workspace, WorkspaceError

DAY_MS = 86_400_000


def parse_utc_boundary(text: str) -> int:
    """ISO date (UTC midnight) or ISO datetime with timezone -> epoch ms."""

    try:
        if len(text) == 10:
            dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("timezone required")
    except ValueError as exc:
        raise WorkspaceError(
            f"invalid time {text!r}: use YYYY-MM-DD or an ISO datetime with zone"
        ) from exc
    return int(dt.timestamp() * 1000)


def default_window(
    now_ms: int, lookback_days: int = DEFAULT_SETTINGS.sync_lookback_days
) -> TimeWindow:
    """Re-pull the last N UTC days (default 3) to absorb provider revisions."""

    today = (now_ms // DAY_MS) * DAY_MS
    return TimeWindow(start_ms=today - lookback_days * DAY_MS, end_ms=today + DAY_MS)


@dataclass
class SyncResult:
    provider: Provider
    window: TimeWindow
    imports: list[ImportResult]
    staged_dir: Path


def run_sync(
    storage: Storage,
    workspace: Workspace,
    *,
    provider: Provider,
    window: TimeWindow,
    admin_key: str,
    scope_id: str,
    now_ms: int,
    scope_filter: Sequence[str] | None = None,
    dedicated_scope: bool = False,
    client: httpx.Client | None = None,
    sleeper: Callable[[float], None] | None = None,
    base_url: str | None = None,
) -> SyncResult:
    if window.end_ms <= window.start_ms:
        raise WorkspaceError("--end must be after --start")
    owned_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(DEFAULT_SETTINGS.http_timeout_s))
    try:
        source: OpenAIReportSource | AnthropicReportSource
        if provider is Provider.openai:
            source = OpenAIReportSource(
                admin_key=admin_key,
                client=client,
                project_ids=scope_filter,
                sleeper=sleeper,
                **({"base_url": base_url} if base_url else {}),
            )
            filter_key = "project_ids"
        elif provider is Provider.anthropic:
            source = AnthropicReportSource(
                admin_key=admin_key,
                client=client,
                workspace_ids=scope_filter,
                sleeper=sleeper,
                **({"base_url": base_url} if base_url else {}),
            )
            filter_key = "workspace_ids"
        else:
            raise WorkspaceError(f"billing sync does not support provider {provider.value}")

        pulls: list[tuple[RecordKind, list[dict[str, Any]], str, str]] = []
        for kind, fetch in (
            (RecordKind.provider_usage, source.fetch_usage),
            (RecordKind.provider_cost, source.fetch_costs),
        ):
            records, describe, grain = fetch(window)  # all pages, or an exception
            pulls.append((kind, records, describe, grain))
    finally:
        if owned_client:
            client.close()

    imports: list[ImportResult] = []
    stamp = datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    staged_dir = workspace.provider_dir / "sync"
    for kind, records, describe, grain in pulls:
        snapshot_id = f"snap_{provider.value}_{kind.value}_{stamp}"
        folder = staged_dir / snapshot_id
        folder.mkdir(parents=True, exist_ok=True)
        body = dumps_records(records)
        records_path = folder / "records.json"
        records_path.write_text(body, encoding="utf-8", newline="\n")
        manifest = ProviderSnapshotManifest(
            snapshot_id=snapshot_id,
            provider=provider,
            scope_id=scope_id,
            record_kind=kind,
            data_kind=DataKind.live,
            grain=grain,
            query_window=window,
            fetched_at_ms=now_ms,
            source_ref=describe[:500],
            source_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            finality=Finality.provisional,
            snapshot_complete=True,
            record_count=len(records),
            currency="USD",
            source_type="api_poller",
            scope_filter={filter_key: ",".join(scope_filter)} if scope_filter else None,
            scope_dedicated=dedicated_scope,
            notes=(
                "Provider-reported data; provisional until the provider stops revising it. "
                "Not an invoice."
            ),
        )
        manifest_path = folder / "manifest.json"
        manifest_path.write_text(
            json.dumps(json.loads(manifest.model_dump_json()), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        imports.append(
            import_snapshot(
                storage,
                file_path=records_path,
                manifest_path=manifest_path,
                now_ms=now_ms,
                workspace_data_kind=DataKind.live,
            )
        )
    return SyncResult(provider=provider, window=window, imports=imports, staged_dir=staged_dir)
