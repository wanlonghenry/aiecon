"""Build the Report model and render JSON + self-contained HTML (PLAN.md §14).

A report is only as current as the runs it selects. Before rendering, the active pricing run
is checked against the calls now in the dataset and the active reconcile run against the
pricing run and the provider snapshots now on file; a mismatch is refused unless the caller
explicitly allows a stale render, which is then banner-marked and listed in the identity
section. Input hashes are keyed by workspace-relative paths so two files with the same name in
different folders never collapse into one row, and only this dataset's inputs are listed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from importlib import resources
from pathlib import Path

from jinja2 import Environment, FunctionLoader, Undefined, select_autoescape

from aiecon import __version__
from aiecon.context_econ import analyze
from aiecon.detectors import (
    detect_discarded_attempts,
    detect_failed_runs,
    detect_repeated_context,
    detect_unused_fallbacks,
    load_inputs,
    summarize,
)
from aiecon.detectors.summary import outcome_economics
from aiecon.pricing.catalog import CatalogIndex
from aiecon.pricing.run import calls_hash, load_run_catalog
from aiecon.reconcile import (
    ReconcileRunManifest,
    day_label,
    effective_provider_records,
    shareable_line,
)
from aiecon.spec.call import UsageCompleteness
from aiecon.spec.common import SCHEMA_VERSION, TimeWindow
from aiecon.spec.finding import ContextRecommendation
from aiecon.spec.pricing import LineItemStatus, PricingRunManifest
from aiecon.spec.reconcile import ComparisonKind
from aiecon.spec.report import (
    ContextSection,
    CostGroup,
    LimitationsSection,
    MonthlyProjection,
    OpportunitiesSection,
    OutcomeSection,
    ReconciliationSection,
    Report,
    ReportIdentity,
    ReportManifest,
    UsageCoverageSection,
    WasteSection,
)
from aiecon.storage import Storage, WorkspaceError

TWELVE = Decimal("1.000000000000")


@dataclass
class ReportPaths:
    html: Path
    json: Path
    manifest: Path


def _q(value: Decimal) -> Decimal:
    return value.quantize(TWELVE, rounding=ROUND_HALF_EVEN)


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and len(commit) == 40 else None


# ------------------------------------------------------------------ staleness
def stale_inputs(
    storage: Storage,
    dataset_id: str,
    pricing: PricingRunManifest | None,
    recon_manifest: ReconcileRunManifest | None,
) -> list[str]:
    """Why the selected runs no longer describe the workspace; empty when they do.

    * pricing: the priced call set (count and content hash) differs from the dataset now
    * reconcile: it was computed against another pricing run, or the snapshots that would be
      selected for its window today differ from the ones it used
    """

    stale: list[str] = []
    if pricing is not None:
        count, digest = calls_hash(storage, dataset_id)
        if count != pricing.call_count or digest != pricing.calls_hash:
            stale.append(
                f"pricing run {pricing.pricing_run_id} priced {pricing.call_count} calls but the "
                f"dataset now holds {count} with different content; run estimate again"
            )
    if recon_manifest is not None:
        active_pricing = pricing.pricing_run_id if pricing else None
        if recon_manifest.pricing_run_id != active_pricing:
            stale.append(
                f"reconcile run {recon_manifest.reconcile_run_id} used pricing run "
                f"{recon_manifest.pricing_run_id or 'none'}, the active one is "
                f"{active_pricing or 'none'}; run reconcile again"
            )
        effective = effective_provider_records(
            storage, data_kind=recon_manifest.data_kind, window=recon_manifest.window
        )
        if effective.snapshot_ids != list(recon_manifest.provider_snapshot_ids):
            stale.append(
                f"provider snapshots changed since reconcile run "
                f"{recon_manifest.reconcile_run_id}; run reconcile again"
            )
    return stale


def _display_path(path_text: str, root: Path | None) -> str:
    """Workspace-relative POSIX path when the file is inside the workspace, else its tail."""

    path = Path(path_text)
    if root is not None:
        try:
            return path.resolve().relative_to(Path(root).resolve()).as_posix()
        except (ValueError, OSError):
            pass
    parts = path.parts[-2:] if len(path.parts) >= 2 else path.parts
    return "/".join(parts)


def validation_lines(data_kind: str, cost_buckets: list, usage_buckets: list) -> list[str]:
    """One line per provider saying what was actually compared in this report.

    Synthetic data never claims live validation. Live data claims exactly the comparisons
    present in the selected reconcile run: monetary buckets with a provider amount, usage
    buckets with provider usage; everything else is "pending".
    """

    lines = []
    for provider in ("openai", "anthropic"):
        if data_kind == "synthetic":
            lines.append(f"{provider}: fixture-verified parsing; live reconciliation pending")
            continue
        money = [
            b
            for b in cost_buckets
            if b.provider.value == provider and b.provider_cost_usd is not None
        ]
        usage = [
            b
            for b in usage_buckets
            if b.provider.value == provider and b.provider_usage is not None
        ]
        parts = []
        if money:
            counts = Counter(b.status.value for b in money)
            days = len({b.window_start_ms for b in money})
            summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            parts.append(f"money compared on {days} UTC day(s): {summary}")
        else:
            parts.append("money: live reconciliation pending (no provider cost in this window)")
        if usage:
            counts = Counter(b.status.value for b in usage)
            summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            parts.append(f"usage compared: {summary}")
        else:
            parts.append("usage: no provider usage report in this window")
        lines.append(f"{provider}: " + "; ".join(parts))
    return lines


# ------------------------------------------------------------------- building
def build_report(
    storage: Storage,
    dataset_id: str,
    *,
    now_ms: int,
    workspace_id: str | None = None,
    monthly_requests: int | None = None,
    validation_status: list[str] | None = None,
    workspace_root: Path | None = None,
    allow_stale: bool = False,
) -> Report:
    inputs = load_inputs(storage, dataset_id)
    calls = inputs.calls
    if not calls:
        raise ValueError(f"dataset {dataset_id} has no calls")
    data_kind = calls[0].data_kind
    pricing = storage.active_pricing_run(dataset_id)
    catalog = load_run_catalog(storage, pricing.pricing_run_id) if pricing else None
    index = CatalogIndex(catalog) if catalog else None
    recon_run = storage.active_run("reconcile", dataset_id)
    recon_manifest = ReconcileRunManifest.model_validate_json(recon_run[1]) if recon_run else None

    stale = stale_inputs(storage, dataset_id, pricing, recon_manifest)
    if stale and not allow_stale:
        raise WorkspaceError(
            "report inputs are stale: " + " | ".join(stale) + " (or pass --allow-stale to "
            "render anyway with a STALE INPUTS banner)"
        )

    starts = [c.started_at_ms for c in calls if c.started_at_ms is not None]
    ends = [c.ended_at_ms or c.started_at_ms or 0 for c in calls]
    window = TimeWindow(start_ms=min(starts), end_ms=max(ends) + 1) if starts else None

    # ------------------------------------------------------------ outcomes / cost
    econ = outcome_economics(inputs)
    groups: dict[tuple, dict] = defaultdict(
        lambda: {"calls": 0, "cost": Decimal(0), "unpriced": 0, "input": 0, "output": 0}
    )
    unpriced_reasons: Counter[str] = Counter()
    calls_unpriced = 0
    for call in calls:
        cost = inputs.cost(call)
        key = (
            call.workflow_id,
            call.node_id,
            call.provider.value,
            call.model_resolved or call.model_requested,
        )
        g = groups[key]
        g["calls"] += 1
        g["cost"] += cost.known_usd
        if not cost.complete:
            g["unpriced"] += 1
            calls_unpriced += 1
        g["input"] += call.input_total_tokens or 0
        g["output"] += call.output_tokens or 0
        for li in inputs.items_by_call.get(call.call_id, []):
            if li.status is LineItemStatus.unpriced and li.unpriced_reason:
                unpriced_reasons[li.unpriced_reason] += 1
    cost_breakdown = [
        CostGroup(
            workflow_id=k[0],
            node_id=k[1],
            provider=k[2],
            model=k[3],
            calls=v["calls"],
            known_cost_usd=_q(v["cost"]),
            unpriced_calls=v["unpriced"],
            input_total_tokens=v["input"],
            output_tokens=v["output"],
        )
        for k, v in sorted(groups.items(), key=lambda kv: (-kv[1]["cost"], str(kv[0])))
    ]

    # ------------------------------------------------------------ reconciliation
    buckets = storage.list_buckets(recon_run[0]) if recon_run else []
    cost_buckets = [b for b in buckets if b.comparison_kind is ComparisonKind.cost_comparison]
    usage_buckets = [b for b in buckets if b.comparison_kind is ComparisonKind.usage_comparison]
    reconciliation = ReconciliationSection(
        ran=recon_run is not None,
        note=None if recon_run else "reconcile has not been run for this dataset",
        buckets=cost_buckets,
        summary_lines=[shareable_line(b) for b in cost_buckets],
        total_local_estimate_usd=recon_manifest.total_local_estimate_usd
        if recon_manifest
        else None,
        total_provider_cost_usd=recon_manifest.total_provider_cost_usd if recon_manifest else None,
        total_unmodeled_charges_usd=(
            recon_manifest.total_unmodeled_charges_usd if recon_manifest else None
        ),
        status_counts=recon_manifest.status_counts if recon_manifest else {},
    )
    local_usage: Counter[str] = Counter()
    provider_usage: Counter[str] = Counter()
    for b in usage_buckets:
        local_usage.update(b.local_usage or {})
        provider_usage.update(b.provider_usage or {})
    usage_coverage = UsageCoverageSection(
        buckets=usage_buckets,
        local_usage=dict(local_usage),
        provider_usage=dict(provider_usage),
        calls_missing_usage=sum(
            1
            for c in calls
            if c.usage_completeness in (UsageCompleteness.missing, UsageCompleteness.invalid)
        ),
        calls_unpriced=calls_unpriced,
        unpriced_reasons=dict(sorted(unpriced_reasons.items())),
        unmatched_scopes=sorted(
            {b.scope_id for b in buckets if b.status.value == "scope_mismatch"}
        ),
    )

    # ------------------------------------------------------------ context + waste
    context = analyze(calls, index, dataset_id=dataset_id) if index is not None else None
    findings = detect_discarded_attempts(inputs) + detect_failed_runs(inputs)
    findings += detect_unused_fallbacks(inputs)
    if context is not None:
        findings += detect_repeated_context(inputs, context)
    summary = summarize(inputs, findings)
    projections: list[MonthlyProjection] = []
    if context is not None and monthly_requests:
        for group in context.groups:
            if (
                group.recommendation is not ContextRecommendation.beneficial
                or not group.modeled_savings_usd
            ):
                continue
            scale = (Decimal(monthly_requests) / Decimal(group.calls)).quantize(
                Decimal("1.0000"), rounding=ROUND_HALF_EVEN
            )
            projections.append(
                MonthlyProjection(
                    requests_per_month=monthly_requests,
                    observed_calls_in_window=group.calls,
                    scale_factor=scale,
                    projected_savings_usd=_q(group.modeled_savings_usd * scale),
                    group_id=group.group_id,
                    assumptions=[
                        "traffic mix, prefix reuse pattern and cache behaviour stay as observed",
                        "prices stay at the catalog values used for the pricing run",
                        "projected scenario, not an observation",
                    ],
                )
            )
    context_section = ContextSection(
        groups=context.groups if context else [],
        calls_with_prefix_evidence=context.calls_with_prefix_evidence if context else 0,
        calls_without_prefix_evidence=context.calls_without_prefix_evidence
        if context
        else len(calls),
        projections=projections,
        note=None if index is not None else "no pricing run with a catalog; scenarios unavailable",
    )

    # --------------------------------------------------------------- limitations
    ingest_rows = storage.query(
        "SELECT file_path, file_sha256 FROM meta_ingest_files WHERE dataset_id = ? "
        "ORDER BY file_path",
        [dataset_id],
    )
    input_hashes: dict[str, str] = {}
    for file_path, digest in ingest_rows:
        key = _display_path(file_path, workspace_root)
        if key in input_hashes and input_hashes[key] != digest:
            key = Path(file_path).as_posix()  # same tail, different file: keep both rows
        input_hashes[key] = digest
    snapshots = [s for s in storage.list_snapshots() if s.data_kind is data_kind]
    for snap in snapshots:
        input_hashes[f"snapshot:{snap.snapshot_id}"] = snap.source_hash
    used_snapshot_ids = (
        list(recon_manifest.provider_snapshot_ids)
        if recon_manifest
        else [s.snapshot_id for s in snapshots]
    )
    used_snapshots = [s for s in snapshots if s.snapshot_id in set(used_snapshot_ids)]
    price_sources = sorted({r.source_url for r in catalog.records}) if catalog else []
    notes = [
        "Estimates are provider-reported usage x catalog prices (evidence class B); they are "
        "never overwritten to match provider totals.",
        "Provider-reported cost is not an invoice; settled amounts appear only when a "
        "settlement file was imported.",
        "Joint savings across findings are not computed; only the best single action is shown.",
        "Context-economics savings are incremental to the cache usage already observed and are "
        "not additive across groups.",
    ]
    if stale:
        notes.insert(0, "STALE INPUTS: " + " | ".join(stale))
    if data_kind.value == "synthetic":
        notes.insert(
            0, "All data in this report is synthetic and demonstrates the computation path only."
        )
    limitations = LimitationsSection(
        validation_status=validation_status
        or validation_lines(data_kind.value, cost_buckets, usage_buckets),
        unpriced_reasons=dict(sorted(unpriced_reasons.items())),
        unsupported_charges_usd=reconciliation.total_unmodeled_charges_usd,
        provisional_snapshots=sum(1 for s in used_snapshots if s.finality.value == "provisional"),
        input_file_hashes=input_hashes,
        price_sources=price_sources,
        notes=notes,
    )

    identity = ReportIdentity(
        dataset_id=dataset_id,
        data_kind=data_kind,
        workspace_id=workspace_id,
        generated_at_ms=now_ms,
        window=window,
        aiecon_version=__version__,
        schema_version=SCHEMA_VERSION,
        normalizer_version=pricing.normalizer_version if pricing else None,
        pricing_run_id=pricing.pricing_run_id if pricing else None,
        catalog_version=pricing.catalog_version if pricing else None,
        catalog_kind=pricing.catalog_kind if pricing else None,
        reconcile_run_id=recon_run[0] if recon_run else None,
        provider_snapshot_ids=used_snapshot_ids,
        data_sources=sorted(
            {e.split("_")[0] for c in calls for e in [c.normalizer_version]} and {"envelope_jsonl"}
        ),
        finality_counts=dict(Counter(s.finality.value for s in used_snapshots)),
        call_count=len(calls),
        outcome_count=len(inputs.outcomes),
        stale_inputs=stale,
    )
    return Report(
        identity=identity,
        outcome_economics=OutcomeSection(
            cohort_definition=(
                "workflow runs with a terminal outcome (succeeded/failed/abandoned); "
                "all calls of those runs, by run terminal time"
            ),
            cohort_runs=econ.cohort_runs,
            succeeded=econ.succeeded,
            failed=econ.failed,
            abandoned=econ.abandoned,
            pending_or_unknown_runs=econ.pending_or_unknown_runs,
            runs_without_outcome=econ.runs_without_outcome,
            unattributed_calls=econ.unattributed_calls,
            cohort_call_count=econ.cohort_call_count,
            cohort_known_cost_usd=econ.cohort_known_cost_usd,
            unknown_cost_calls=econ.unknown_cost_calls,
            cost_per_successful_outcome_usd=econ.cost_per_successful_outcome_usd,
            successful_runs_only_cost_usd=econ.successful_runs_only_cost_usd,
            successful_runs_only_per_success_usd=econ.successful_runs_only_per_success_usd,
            is_lower_bound=econ.is_lower_bound,
            note=econ.note,
        ),
        cost_breakdown=cost_breakdown,
        monetary_reconciliation=reconciliation,
        usage_coverage=usage_coverage,
        waste_findings=WasteSection(findings=findings, by_detector=summary.by_detector),
        context_economics=context_section,
        opportunities=OpportunitiesSection(
            unique_flagged_cost_usd=summary.unique_flagged_cost_usd,
            unique_flagged_line_items=summary.unique_flagged_line_items,
            unique_flagged_calls=summary.unique_flagged_calls,
            unknown_cost_flagged_calls=summary.unknown_cost_flagged_calls,
            best_single_action_savings_usd=summary.best_single_action_savings_usd,
            best_single_action_finding_id=summary.best_single_action_finding_id,
            joint_savings=summary.joint_savings,
        ),
        limitations=limitations,
    )


# ------------------------------------------------------------------ rendering
def _missing(value: object) -> bool:
    """None, empty and Jinja's Undefined (a key absent from a dict) all render as unknown."""

    return value is None or value == "" or isinstance(value, Undefined)


def _usd(value: object, places: int = 6) -> str:
    if _missing(value):
        return "Unknown"
    dec = Decimal(str(value))
    text = f"{dec:.{places}f}"
    return f"${text}"


def _pct(value: object) -> str:
    if _missing(value):
        return "Not comparable"
    return f"{Decimal(str(value)):+.2f}%"


def _num(value: object) -> str:
    if _missing(value):
        return "Not available"
    return f"{int(value):,}"


def _day(ts_ms: object) -> str:
    if _missing(ts_ms):
        return "Unknown"
    return day_label(int(ts_ms))


def _ts(ts_ms: object) -> str:
    if _missing(ts_ms):
        return "Unknown"
    from datetime import UTC, datetime

    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _load_template(name: str) -> str | None:
    try:
        return resources.files("aiecon").joinpath("templates").joinpath(name).read_text("utf-8")
    except (FileNotFoundError, OSError):
        return None


def render_html(report: Report) -> str:
    env = Environment(
        loader=FunctionLoader(_load_template),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["usd"] = _usd
    env.filters["pct"] = _pct
    env.filters["num"] = _num
    env.filters["day"] = _day
    env.filters["ts"] = _ts
    data = json.loads(report.model_dump_json())  # decimals as strings, same as report.json
    bars = _cost_bars(data["cost_breakdown"])
    return env.get_template("report.html.j2").render(report=data, bars=bars)


def _cost_bars(groups: list[dict]) -> list[dict]:
    if not groups:
        return []
    top = groups[:8]
    maximum = max(Decimal(g["known_cost_usd"]) for g in top) or Decimal(1)
    bars = []
    for g in top:
        width = int(Decimal(g["known_cost_usd"]) / maximum * 100) if maximum > 0 else 0
        label = " / ".join(str(x) for x in (g["workflow_id"], g["node_id"], g["model"]) if x)
        bars.append({"label": label, "width": max(width, 1), "value": g["known_cost_usd"]})
    return bars


def write_report(
    report: Report,
    out_html: Path,
    *,
    now_ms: int,
    catalog_hash: str | None,
) -> tuple[ReportPaths, ReportManifest]:
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_json = out_html.with_suffix(".json")
    out_manifest = out_html.with_name(out_html.stem + "-manifest.json")
    json_text = report.model_dump_json(indent=2) + "\n"
    html_text = render_html(report)
    out_json.write_text(json_text, encoding="utf-8", newline="\n")
    out_html.write_text(html_text, encoding="utf-8", newline="\n")
    manifest = ReportManifest(
        report_id=f"rep_{hashlib.sha256(json_text.encode('utf-8')).hexdigest()[:16]}",
        dataset_id=report.identity.dataset_id,
        data_kind=report.identity.data_kind,
        generated_at_ms=now_ms,
        git_commit=_git_commit(),
        aiecon_version=__version__,
        schema_version=SCHEMA_VERSION,
        normalizer_version=report.identity.normalizer_version,
        catalog_version=report.identity.catalog_version,
        catalog_hash=catalog_hash,
        pricing_run_id=report.identity.pricing_run_id,
        reconcile_run_id=report.identity.reconcile_run_id,
        provider_snapshot_ids=report.identity.provider_snapshot_ids,
        window=report.identity.window,
        input_file_hashes=report.limitations.input_file_hashes,
        report_json_sha256=hashlib.sha256(json_text.encode("utf-8")).hexdigest(),
        report_html_sha256=hashlib.sha256(html_text.encode("utf-8")).hexdigest(),
    )
    out_manifest.write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return ReportPaths(html=out_html, json=out_json, manifest=out_manifest), manifest
