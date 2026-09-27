"""Shared orchestration for demo / init / ingest / doctor (PLAN.md section 10).

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
from aiecon.ingest import IngestStats, ingest_paths
from aiecon.spec.common import SCHEMA_VERSION, DataKind
from aiecon.storage import Storage, Workspace, WorkspaceError, WorkspaceManifest

DEMO_DATASET_ID = "demo-support-v1"
DEMO_WORKSPACE_ID = "ws_demo_support_v1"
DEMO_FIXTURE_DIR = "demo_support_v1"
DEMO_BANNER = "SYNTHETIC DEMO"


def fixture_root() -> Path:
    """Location of the packaged synthetic fixtures (works from a wheel too)."""

    return Path(str(resources.files("aiecon.data").joinpath(DEMO_FIXTURE_DIR)))


def load_expected_metrics() -> dict[str, Any]:
    return json.loads((fixture_root() / "expected_metrics.json").read_text("utf-8"))


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


# -------------------------------------------------------------------------- demo
@dataclass
class DemoResult:
    workspace: Path
    ingest: IngestStats
    calls: int
    outcomes: int
    expected_calls: int
    expected_outcomes: int
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
    """Fixture load -> ingest (-> estimate -> import -> reconcile -> detect -> report).

    Later stages are attached as their tasks land; each one records itself in ``stages``.
    """

    workspace = _prepare_demo_workspace(out_dir, now_ms=now_ms)
    expected = load_expected_metrics()
    stages: list[str] = []

    raw_day = workspace.raw_dir / "2026-09-26"
    raw_day.mkdir(parents=True, exist_ok=True)
    events_src = fixture_root() / "events.jsonl"
    events_dst = raw_day / "events.jsonl"
    shutil.copyfile(events_src, events_dst)
    provider_dst = workspace.provider_dir / "fixtures"
    shutil.copytree(fixture_root() / "provider", provider_dst)
    stages.append("fixtures_loaded")

    with workspace.lock(), Storage.open(workspace.db_path) as storage:
        storage.apply_schema()
        stats = ingest_paths(
            storage, [workspace.raw_dir], now_ms=now_ms, workspace_data_kind=DataKind.synthetic
        )
        stages.append("ingested")
        calls = storage.count_calls(DEMO_DATASET_ID)
        outcomes = storage.count_outcomes(DEMO_DATASET_ID)

    return DemoResult(
        workspace=workspace.root,
        ingest=stats,
        calls=calls,
        outcomes=outcomes,
        expected_calls=int(expected["calls"]),
        expected_outcomes=int(expected["runs"]),
        stages=stages,
        outputs={"database": workspace.db_path},
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
        "provider/openai_cost.json",
        "provider/openai_usage.json",
        "provider/anthropic_cost.json",
        "provider/anthropic_usage.json",
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
