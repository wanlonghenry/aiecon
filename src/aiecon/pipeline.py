"""Shared orchestration for demo / init / ingest / estimate / import / doctor (PLAN.md §10).

Every CLI command goes through these functions so the demo and the live path exercise the
same code. Functions take ``now_ms`` explicitly; nothing here reads the clock.
"""

from __future__ import annotations

import importlib
import json
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from aiecon import __version__
from aiecon.billing.importer import ImportResult, import_snapshot
from aiecon.billing.sync import SyncResult, parse_utc_boundary, run_sync
from aiecon.config import ADMIN_KEY_ENVS
from aiecon.ingest import IngestStats, ingest_paths
from aiecon.pricing.catalog import load_catalog, load_synthetic_catalog
from aiecon.pricing.run import PricingRunResult, run_pricing
from aiecon.reconcile import DAY_MS, ReconcileResult, reconcile
from aiecon.report import ReportPaths, build_report, write_report
from aiecon.spec.common import SCHEMA_VERSION, DataKind, Provider, TimeWindow
from aiecon.spec.pricing import PriceCatalog
from aiecon.spec.report import ReportManifest
from aiecon.storage import Storage, Workspace, WorkspaceError, WorkspaceManifest

DEMO_DATASET_ID = "demo-support-v1"
DEMO_WORKSPACE_ID = "ws_demo_support_v1"
DEMO_FIXTURE_DIR = "demo_support_v1"
DEMO_BANNER = "SYNTHETIC DEMO"
PROVIDER_FIXTURES = ("openai_usage", "openai_cost", "anthropic_usage", "anthropic_cost")


def fixture_root() -> Path:
    """Location of the packaged synthetic fixtures (works from a wheel too)."""

    return Path(str(resources.files("aiecon.data").joinpath(DEMO_FIXTURE_DIR)))


def load_expected_metrics() -> dict[str, Any]:
    return json.loads((fixture_root() / "expected_metrics.json").read_text("utf-8"))


def resolve_dataset_id(storage: Storage, requested: str | None) -> str:
    datasets = storage.list_dataset_ids()
    if requested is not None:
        if requested not in datasets:
            known = ", ".join(datasets) or "none"
            raise WorkspaceError(f"dataset {requested!r} has no ingested data (known: {known})")
        return requested
    if not datasets:
        raise WorkspaceError("no data ingested yet; run ingest first")
    if len(datasets) > 1:
        raise WorkspaceError(
            f"workspace holds several datasets ({', '.join(datasets)}); pass --dataset-id"
        )
    return datasets[0]


def _check_catalog_kind(manifest: WorkspaceManifest, catalog: PriceCatalog) -> None:
    if manifest.data_kind is DataKind.synthetic and catalog.catalog_kind != "synthetic":
        raise WorkspaceError("a synthetic workspace must be priced with a synthetic catalog")
    if manifest.data_kind is DataKind.live and catalog.catalog_kind != "real":
        raise WorkspaceError("a live workspace must be priced with a real (verified) catalog")


# -------------------------------------------------------------------------- init
def run_init(workspace_path: Path, *, data_kind: DataKind, now_ms: int) -> WorkspaceManifest:
    workspace = Workspace(workspace_path)
    return workspace.init(data_kind=data_kind, now_ms=now_ms, aiecon_version=__version__)


# ------------------------------------------------------------------------ ingest
def run_ingest(workspace_path: Path, inputs: Iterable[Path], *, now_ms: int) -> IngestStats:
    workspace = Workspace(workspace_path)
    manifest = workspace.load_manifest()
    with workspace.lock(), Storage.open(workspace.db_path) as storage:
        storage.apply_schema()
        return ingest_paths(storage, inputs, now_ms=now_ms, workspace_data_kind=manifest.data_kind)


# ---------------------------------------------------------------------- estimate
def run_estimate(
    workspace_path: Path, catalog_path: Path, *, now_ms: int, dataset_id: str | None = None
) -> PricingRunResult:
    workspace = Workspace(workspace_path)
    manifest = workspace.load_manifest()
    catalog = load_catalog(catalog_path)
    _check_catalog_kind(manifest, catalog)
    with workspace.lock(), Storage.open(workspace.db_path) as storage:
        storage.apply_schema()
        dataset = resolve_dataset_id(storage, dataset_id)
        return run_pricing(storage, dataset, catalog, now_ms=now_ms)


# ---------------------------------------------------------------- billing import
def run_billing_import(
    workspace_path: Path, *, file_path: Path, manifest_path: Path, now_ms: int
) -> ImportResult:
    workspace = Workspace(workspace_path)
    manifest = workspace.load_manifest()
    with workspace.lock(), Storage.open(workspace.db_path) as storage:
        storage.apply_schema()
        return import_snapshot(
            storage,
            file_path=file_path,
            manifest_path=manifest_path,
            now_ms=now_ms,
            workspace_data_kind=manifest.data_kind,
        )


def _window(start: str, end: str) -> TimeWindow:
    window = TimeWindow(start_ms=parse_utc_boundary(start), end_ms=parse_utc_boundary(end))
    if window.end_ms <= window.start_ms:
        raise WorkspaceError("--end must be after --start")
    return window


# ------------------------------------------------------------------ billing sync
def run_billing_sync(
    workspace_path: Path,
    *,
    provider: str,
    start: str,
    end: str,
    scope_id: str | None,
    scope_filter: list[str] | None,
    now_ms: int,
) -> SyncResult:
    import os

    workspace = Workspace(workspace_path)
    manifest = workspace.load_manifest()
    if manifest.data_kind is not DataKind.live:
        raise WorkspaceError("billing sync pulls real provider data; use a live workspace")
    try:
        prov = Provider(provider)
    except ValueError:
        raise WorkspaceError("--provider must be openai or anthropic") from None
    env_name = ADMIN_KEY_ENVS.get(prov.value)
    if env_name is None:
        raise WorkspaceError(f"billing sync does not support provider {provider}")
    admin_key = os.environ.get(env_name)  # read once, passed to the poller, never printed
    if not admin_key:
        raise WorkspaceError(
            f"{env_name} is not set; billing sync needs the provider's admin key "
            "(model API keys cannot read usage or cost reports)"
        )
    window = _window(start, end)
    with workspace.lock(), Storage.open(workspace.db_path) as storage:
        storage.apply_schema()
        return run_sync(
            storage,
            workspace,
            provider=prov,
            window=window,
            admin_key=admin_key,
            scope_id=scope_id or prov.value,
            now_ms=now_ms,
            scope_filter=scope_filter,
        )


# --------------------------------------------------------------------- reconcile
def run_reconcile(
    workspace_path: Path,
    *,
    start: str,
    end: str,
    dataset_id: str | None,
    now_ms: int,
) -> ReconcileResult:
    workspace = Workspace(workspace_path)
    workspace.load_manifest()
    window = _window(start, end)
    with workspace.lock(), Storage.open(workspace.db_path) as storage:
        storage.apply_schema()
        dataset = resolve_dataset_id(storage, dataset_id)
        return reconcile(storage, dataset, window, now_ms=now_ms)


def dataset_day_window(storage: Storage, dataset_id: str) -> TimeWindow:
    """Whole UTC days covering every call of the dataset."""

    first, last = storage.query(
        "SELECT MIN(started_at_ms), MAX(COALESCE(ended_at_ms, started_at_ms)) FROM calls "
        "WHERE dataset_id = ?",
        [dataset_id],
    )[0]
    if first is None:
        raise WorkspaceError("no calls to reconcile")
    return TimeWindow(
        start_ms=(int(first) // DAY_MS) * DAY_MS, end_ms=(int(last) // DAY_MS + 1) * DAY_MS
    )


# ------------------------------------------------------------------------ report
def run_report(
    workspace_path: Path,
    *,
    out: Path,
    now_ms: int,
    dataset_id: str | None,
    monthly_requests: int | None,
) -> tuple[ReportPaths, ReportManifest]:
    workspace = Workspace(workspace_path)
    manifest = workspace.load_manifest()
    with workspace.lock(), Storage.open(workspace.db_path) as storage:
        storage.apply_schema()
        dataset = resolve_dataset_id(storage, dataset_id)
        pricing = storage.active_pricing_run(dataset)
        if pricing is None:
            raise WorkspaceError("no pricing run for this dataset; run estimate first")
        report = build_report(
            storage,
            dataset,
            now_ms=now_ms,
            workspace_id=manifest.workspace_id,
            monthly_requests=monthly_requests,
        )
        return write_report(report, out, now_ms=now_ms, catalog_hash=pricing.catalog_hash)


# -------------------------------------------------------------------------- demo
@dataclass
class DemoResult:
    workspace: Path
    ingest: IngestStats
    calls: int
    outcomes: int
    expected_calls: int
    expected_outcomes: int
    pricing: PricingRunResult | None = None
    imports: list[ImportResult] = field(default_factory=list)
    reconciliation: ReconcileResult | None = None
    stages: list[str] = field(default_factory=list)
    outputs: dict[str, Path] = field(default_factory=dict)

    @property
    def counts_match_expected(self) -> bool:
        return self.calls == self.expected_calls and self.outcomes == self.expected_outcomes


def _prepare_demo_workspace(out_dir: Path, *, now_ms: int) -> Workspace:
    workspace = Workspace(out_dir)
    if workspace.exists:
        manifest = workspace.load_manifest()
        if (
            manifest.data_kind is not DataKind.synthetic
            or manifest.workspace_id != DEMO_WORKSPACE_ID
        ):
            raise WorkspaceError(
                f"{out_dir} is not a demo workspace (workspace_id={manifest.workspace_id}, "
                f"data_kind={manifest.data_kind.value}); refusing to overwrite it"
            )
        # rebuild: the raw fixtures are the source of truth, everything derived is dropped
        for path in (workspace.db_path, workspace.db_path.with_suffix(".duckdb.wal")):
            if path.exists():
                path.unlink()
        for folder in (workspace.raw_dir, workspace.provider_dir, workspace.reports_dir):
            if folder.exists():
                shutil.rmtree(folder)
        workspace.manifest_path.unlink()
    elif out_dir.exists() and any(out_dir.iterdir()):
        raise WorkspaceError(
            f"{out_dir} exists and is not an aiecon demo workspace; choose an empty directory"
        )
    workspace.init(
        data_kind=DataKind.synthetic,
        now_ms=now_ms,
        aiecon_version=__version__,
        workspace_id=DEMO_WORKSPACE_ID,
    )
    return workspace


def run_demo(out_dir: Path, *, now_ms: int) -> DemoResult:
    """Fixture load -> ingest -> estimate -> provider import (-> reconcile -> detect -> report).

    Later stages are attached as their tasks land; each one records itself in ``stages``.
    """

    workspace = _prepare_demo_workspace(out_dir, now_ms=now_ms)
    expected = load_expected_metrics()
    stages: list[str] = []

    raw_day = workspace.raw_dir / "2026-09-26"
    raw_day.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture_root() / "events.jsonl", raw_day / "events.jsonl")
    provider_dst = workspace.provider_dir / "fixtures"
    shutil.copytree(fixture_root() / "provider", provider_dst)
    stages.append("fixtures_loaded")

    imports: list[ImportResult] = []
    with workspace.lock(), Storage.open(workspace.db_path) as storage:
        storage.apply_schema()
        stats = ingest_paths(
            storage, [workspace.raw_dir], now_ms=now_ms, workspace_data_kind=DataKind.synthetic
        )
        stages.append("ingested")
        calls = storage.count_calls(DEMO_DATASET_ID)
        outcomes = storage.count_outcomes(DEMO_DATASET_ID)

        pricing = run_pricing(storage, DEMO_DATASET_ID, load_synthetic_catalog(), now_ms=now_ms)
        stages.append("estimated")

        for name in PROVIDER_FIXTURES:
            imports.append(
                import_snapshot(
                    storage,
                    file_path=provider_dst / f"{name}.json",
                    manifest_path=provider_dst / f"{name}.manifest.json",
                    now_ms=now_ms,
                    workspace_data_kind=DataKind.synthetic,
                )
            )
        stages.append("provider_fixtures_imported")

        recon = reconcile(
            storage, DEMO_DATASET_ID, dataset_day_window(storage, DEMO_DATASET_ID), now_ms=now_ms
        )
        stages.append("reconciled")

        report = build_report(
            storage, DEMO_DATASET_ID, now_ms=now_ms, workspace_id=DEMO_WORKSPACE_ID
        )
        paths, _report_manifest = write_report(
            report,
            workspace.root / "report.html",
            now_ms=now_ms,
            catalog_hash=pricing.manifest.catalog_hash,
        )
        stages.append("reported")

    return DemoResult(
        workspace=workspace.root,
        ingest=stats,
        calls=calls,
        outcomes=outcomes,
        expected_calls=int(expected["calls"]),
        expected_outcomes=int(expected["runs"]),
        pricing=pricing,
        imports=imports,
        reconciliation=recon,
        stages=stages,
        outputs={
            "report_html": paths.html,
            "report_json": paths.json,
            "report_manifest": paths.manifest,
            "database": workspace.db_path,
        },
    )


# ------------------------------------------------------------------------ doctor
@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def run_doctor_offline() -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("aiecon", True, f"version {__version__}, schema {SCHEMA_VERSION}"))
    for module in ("pydantic", "duckdb", "typer", "jinja2", "httpx"):
        try:
            mod = importlib.import_module(module)
            checks.append(Check(module, True, getattr(mod, "__version__", "present")))
        except ImportError as exc:  # pragma: no cover - environment dependent
            checks.append(Check(module, False, f"import failed: {type(exc).__name__}"))
    root = fixture_root()
    for relative in (
        "events.jsonl",
        "expected_metrics.json",
        "synthetic-catalog.json",
        *(f"provider/{name}.json" for name in PROVIDER_FIXTURES),
    ):
        path = root / relative
        checks.append(Check(f"fixture {relative}", path.exists(), str(path)))
    try:
        with Storage.open(":memory:") as storage:
            storage.apply_schema()
            tables = storage.query("SELECT COUNT(*) FROM information_schema.tables")[0][0]
        checks.append(Check("duckdb schema", True, f"{tables} tables/views created in memory"))
    except Exception as exc:  # pragma: no cover - defensive
        checks.append(Check("duckdb schema", False, type(exc).__name__))
    try:
        templates = resources.files("aiecon").joinpath("templates")
        has_template = templates.joinpath("report.html.j2").is_file()
        checks.append(
            Check("report template", has_template, "report.html.j2" if has_template else "missing")
        )
    except (FileNotFoundError, ModuleNotFoundError):  # pragma: no cover
        checks.append(Check("report template", False, "templates package missing"))
    return checks
