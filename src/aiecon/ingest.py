"""Idempotent JSONL ingest into the DuckDB projection (PLAN.md §3.2, §4.2, §4.5).

Rules implemented:

* raw JSONL is the source of truth; the database is a rebuildable projection
* same event_id + same content = duplicate (no-op); same event_id + different content =
  conflict (recorded, original kept, CLI exits 3)
* a higher ``revision`` for the same call/run replaces the projection; a lower one is
  recorded as superseded; the same revision from a different event with the same body is a
  repeated terminal state (no-op) and with a different body is a conflict
* a finish arriving before its start, or a start arriving after a finish, never regresses
  the projection to ``in_flight``
* unterminated last lines are ``truncated_tail``; damaged middle lines are counted by line
  number and error type only, never echoed
* an envelope whose ``data_kind`` differs from the workspace is rejected, so synthetic and
  live records never share a database

One file is one transaction. Events are merged in memory against the committed projection
and written back in bulk, so ingest cost is linear in the number of events.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aiecon.adapters.normalize import normalize
from aiecon.privacy import safe_error_summary
from aiecon.spec.call import ModelCall
from aiecon.spec.common import DataKind
from aiecon.spec.envelope import CallStatus, EventType, OutcomePayload, RawEnvelope
from aiecon.spec.outcome import Outcome
from aiecon.storage import Storage


class IngestConflict(Exception):
    """Raised by the CLI layer when conflicts were recorded (exit code 3)."""


@dataclass
class FileIngestResult:
    path: str
    sha256: str
    skipped_unchanged: bool = False
    line_count: int = 0
    accepted: int = 0
    duplicates: int = 0
    conflicts: int = 0
    superseded: int = 0
    repeated_terminal: int = 0
    rejected: int = 0
    truncated_tail: bool = False
    rejected_lines: list[tuple[int, str]] = field(default_factory=list)
    conflict_event_ids: list[str] = field(default_factory=list)


@dataclass
class IngestStats:
    files: list[FileIngestResult] = field(default_factory=list)
    by_event_type: Counter[str] = field(default_factory=Counter)
    datasets: set[str] = field(default_factory=set)

    @property
    def accepted(self) -> int:
        return sum(f.accepted for f in self.files)

    @property
    def duplicates(self) -> int:
        return sum(f.duplicates for f in self.files)

    @property
    def conflicts(self) -> int:
        return sum(f.conflicts for f in self.files)

    @property
    def rejected(self) -> int:
        return sum(f.rejected for f in self.files)

    @property
    def superseded(self) -> int:
        return sum(f.superseded for f in self.files)

    @property
    def repeated_terminal(self) -> int:
        return sum(f.repeated_terminal for f in self.files)

    @property
    def truncated_tail_files(self) -> int:
        return sum(1 for f in self.files if f.truncated_tail)

    @property
    def skipped_files(self) -> int:
        return sum(1 for f in self.files if f.skipped_unchanged)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": len(self.files),
            "skipped_unchanged_files": self.skipped_files,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "repeated_terminal": self.repeated_terminal,
            "superseded": self.superseded,
            "conflicts": self.conflicts,
            "rejected": self.rejected,
            "truncated_tail_files": self.truncated_tail_files,
            "by_event_type": dict(sorted(self.by_event_type.items())),
            "datasets": sorted(self.datasets),
            "rejected_lines": [
                {"file": f.path, "line": line, "error": err}
                for f in self.files
                for line, err in f.rejected_lines
            ][:50],
            "conflict_event_ids": [e for f in self.files for e in f.conflict_event_ids][:50],
        }


# ------------------------------------------------------------------ file reading
def collect_jsonl_paths(inputs: Iterable[Path]) -> list[Path]:
    paths: list[Path] = []
    for item in inputs:
        item = Path(item)
        if item.is_dir():
            paths.extend(sorted(p for p in item.rglob("*.jsonl") if p.is_file()))
        elif item.is_file():
            paths.append(item)
        else:
            raise FileNotFoundError(str(item))
    return paths


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_jsonl_lines(path: Path) -> Iterator[tuple[int, bytes | None, bool]]:
    """Yield ``(line_no, line_bytes, is_unterminated_tail)``.

    The final line without a trailing newline is reported with ``is_unterminated_tail=True``
    and ``line_bytes=None`` so callers never parse a half-written record.
    """

    data = path.read_bytes()
    if not data:
        return
    lines = data.split(b"\n")
    terminated = data.endswith(b"\n")
    if terminated:
        lines = lines[:-1]
    for index, raw in enumerate(lines, start=1):
        is_last = index == len(lines)
        if is_last and not terminated:
            yield index, None, True
            return
        yield index, raw, False


def _parse_json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"), parse_float=Decimal)


# ------------------------------------------------------------------- projection
def _fill_missing(
    primary: ModelCall, secondary: ModelCall, fields: Iterable[str]
) -> dict[str, Any]:
    """Values from ``primary`` win; ``None`` slots are filled from ``secondary``."""

    update: dict[str, Any] = {}
    for name in fields:
        if getattr(primary, name) is None and getattr(secondary, name) is not None:
            update[name] = getattr(secondary, name)
    return update


_START_FIELDS = (
    "started_at_ms",
    "model_resolved",
    "service_tier",
    "inference_region",
    "stream",
    "cache_policy",
    "prefix_fingerprint",
    "fingerprint_key_id",
    "prefix_tokens",
    "prefix_token_count_method",
    "workflow_id",
    "workflow_run_id",
    "node_id",
    "node_run_id",
    "attempt_index",
    "retry_of_call_id",
    "fallback_of_call_id",
    "trace_id",
    "span_id",
)


def _with_provenance(call: ModelCall, existing: ModelCall, update: dict[str, Any]) -> ModelCall:
    update = dict(update)
    update["source_event_ids"] = list(
        dict.fromkeys(existing.source_event_ids + call.source_event_ids)
    )
    update["source_content_hashes"] = list(
        dict.fromkeys(existing.source_content_hashes + call.source_content_hashes)
    )
    return call.model_copy(update=update)


def apply_call_event(
    existing: ModelCall | None, envelope: RawEnvelope
) -> tuple[ModelCall | None, str]:
    """Return ``(new_projection_or_None, action)``.

    Actions: created, merged_start, finished, revised, superseded, repeated_terminal,
    conflict, no_regression.
    """

    incoming = normalize(envelope)
    if existing is None:
        return incoming, "created" if incoming.status is CallStatus.in_flight else "finished"

    if envelope.event_type is EventType.call_started:
        update = _fill_missing(existing, incoming, _START_FIELDS)
        merged = _with_provenance(existing, incoming, update)
        if existing.status is CallStatus.in_flight:
            return merged, "merged_start"
        return merged, "no_regression"

    if existing.status is CallStatus.in_flight:
        update = _fill_missing(incoming, existing, _START_FIELDS)
        return _with_provenance(incoming, existing, update), "finished"

    if incoming.revision > existing.revision:
        update = _fill_missing(incoming, existing, _START_FIELDS)
        return _with_provenance(incoming, existing, update), "revised"
    if incoming.revision < existing.revision:
        return None, "superseded"
    if envelope.body_hash() in existing.source_content_hashes:
        return None, "repeated_terminal"
    return None, "conflict"


def apply_outcome_event(
    existing: Outcome | None, envelope: RawEnvelope
) -> tuple[Outcome | None, str]:
    payload = envelope.payload
    assert isinstance(payload, OutcomePayload)
    ctx = envelope.context
    assert ctx.workflow_run_id is not None
    incoming = Outcome(
        dataset_id=envelope.dataset_id,
        workflow_run_id=ctx.workflow_run_id,
        workflow_id=ctx.workflow_id,
        data_kind=envelope.data_kind,
        revision=envelope.revision,
        terminal_at_ms=payload.terminal_at_ms,
        status=payload.status,
        success=payload.success,
        outcome_source=payload.outcome_source,
        call_dispositions=list(payload.call_dispositions),
        source_event_ids=[envelope.event_id],
        source_content_hashes=[envelope.body_hash()],
    )
    if existing is None:
        return incoming, "created"
    if incoming.revision > existing.revision:
        merged = incoming.model_copy(
            update={
                "source_event_ids": list(
                    dict.fromkeys(existing.source_event_ids + incoming.source_event_ids)
                ),
                "source_content_hashes": list(
                    dict.fromkeys(existing.source_content_hashes + incoming.source_content_hashes)
                ),
            }
        )
        return merged, "revised"
    if incoming.revision < existing.revision:
        return None, "superseded"
    if envelope.body_hash() in existing.source_content_hashes:
        return None, "repeated_terminal"
    return None, "conflict"


# ----------------------------------------------------------------------- ingest
_NOT_APPLIED = {"conflict", "superseded", "repeated_terminal"}


def ingest_file(
    storage: Storage,
    path: Path,
    *,
    now_ms: int,
    workspace_data_kind: DataKind | None,
    stats: IngestStats,
) -> FileIngestResult:
    sha = _sha256_file(path)
    result = FileIngestResult(path=str(path), sha256=sha)
    if storage.file_already_ingested(str(path), sha):
        result.skipped_unchanged = True
        stats.files.append(result)
        return result

    # pass 1: parse and validate every line; nothing touches the database yet
    parsed: list[tuple[int, RawEnvelope]] = []
    for line_no, raw, tail in iter_jsonl_lines(path):
        if tail:
            result.truncated_tail = True
            break
        result.line_count += 1
        if not raw.strip():
            continue
        try:
            obj = _parse_json(raw)
        except (UnicodeDecodeError, ValueError):
            result.rejected += 1
            result.rejected_lines.append((line_no, "json_decode_error"))
            continue
        try:
            envelope = RawEnvelope.model_validate(obj)
        except ValidationError as exc:
            result.rejected += 1
            result.rejected_lines.append((line_no, safe_error_summary(exc)))
            continue
        if workspace_data_kind is not None and envelope.data_kind is not workspace_data_kind:
            result.rejected += 1
            result.rejected_lines.append((line_no, "data_kind_mismatch"))
            continue
        parsed.append((line_no, envelope))

    # pass 2: load the committed state this file touches
    known = storage.load_event_hashes([(e.dataset_id, e.event_id) for _, e in parsed])
    call_keys = [
        (e.dataset_id, e.target_id) for _, e in parsed if e.event_type is not EventType.outcome
    ]
    outcome_keys = [
        (e.dataset_id, e.target_id) for _, e in parsed if e.event_type is EventType.outcome
    ]
    calls = storage.get_calls_many(list(dict.fromkeys(call_keys)))
    outcomes = storage.get_outcomes_many(list(dict.fromkeys(outcome_keys)))
    dirty_calls: dict[tuple[str, str], ModelCall] = {}
    dirty_outcomes: dict[tuple[str, str], Outcome] = {}
    event_rows: list[list[Any]] = []
    dataset_seen: str | None = None

    # pass 3: merge in memory
    for line_no, envelope in parsed:
        dataset_seen = envelope.dataset_id
        stats.datasets.add(envelope.dataset_id)
        key = (envelope.dataset_id, envelope.event_id)
        content_hash = envelope.content_hash()
        seen_hash = known.get(key)
        if seen_hash is not None:
            if seen_hash == content_hash:
                result.duplicates += 1
            else:
                result.conflicts += 1
                result.conflict_event_ids.append(envelope.event_id)
            continue
        known[key] = content_hash

        target = (envelope.dataset_id, envelope.target_id)
        if envelope.event_type is EventType.outcome:
            existing_outcome = dirty_outcomes.get(target) or outcomes.get(target)
            projection, action = apply_outcome_event(existing_outcome, envelope)
            if projection is not None:
                dirty_outcomes[target] = projection
        else:
            existing_call = dirty_calls.get(target) or calls.get(target)
            projection, action = apply_call_event(existing_call, envelope)
            if projection is not None:
                dirty_calls[target] = projection

        if action == "conflict":
            result.conflicts += 1
            result.conflict_event_ids.append(envelope.event_id)
        elif action == "superseded":
            result.superseded += 1
        elif action == "repeated_terminal":
            result.repeated_terminal += 1
        else:
            result.accepted += 1
        stats.by_event_type[envelope.event_type.value] += 1
        event_rows.append(
            [
                envelope.dataset_id,
                envelope.event_id,
                content_hash,
                envelope.event_type.value,
                envelope.revision,
                envelope.target_id,
                path.name,
                line_no,
                now_ms,
                action not in _NOT_APPLIED,
            ]
        )

    # pass 4: one transaction, bulk writes
    with storage.transaction():
        if dirty_calls:
            storage.upsert_calls(dirty_calls.values())
        if dirty_outcomes:
            storage.upsert_outcomes(dirty_outcomes.values())
        if event_rows:
            storage.record_events(event_rows)
        storage.record_ingest_file(
            file_path=str(path),
            file_sha256=sha,
            dataset_id=dataset_seen,
            line_count=result.line_count,
            accepted=result.accepted,
            duplicates=result.duplicates,
            conflicts=result.conflicts,
            rejected=result.rejected,
            truncated_tail=result.truncated_tail,
            processed_at_ms=now_ms,
        )
    stats.files.append(result)
    return result


def ingest_paths(
    storage: Storage,
    inputs: Iterable[Path],
    *,
    now_ms: int,
    workspace_data_kind: DataKind | None,
) -> IngestStats:
    stats = IngestStats()
    for path in collect_jsonl_paths(inputs):
        ingest_file(
            storage, path, now_ms=now_ms, workspace_data_kind=workspace_data_kind, stats=stats
        )
    return stats
