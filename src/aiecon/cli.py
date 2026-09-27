"""aiecon command line (PLAN.md section 10). Parsing and exit codes only; no business math."""

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from aiecon import __version__
from aiecon.billing.http import ProviderReportError
from aiecon.billing.importer import BillingImportError
from aiecon.config import (
    ADMIN_KEY_ENVS,
    FINGERPRINT_KEY_ENV,
    LIVE_MODEL_ENVS,
    MODEL_KEY_ENVS,
    debug_enabled,
    env_present,
    env_value_non_secret,
    now_ms,
    resolve_workspace,
)
from aiecon.pricing.catalog import CatalogError
from aiecon.spec.common import DataKind
from aiecon.storage import WorkspaceError

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2
EXIT_CONTRACT = 3

app = typer.Typer(
    name="aiecon",
    help="Reconstruct AI workflow costs, reconcile them with provider reports, and inspect "
    "evidence-backed waste and context reuse opportunities.",
    no_args_is_help=True,
    add_completion=False,
)
schema_app = typer.Typer(help="JSON Schema export for the aiecon data contracts.")
billing_app = typer.Typer(help="Provider usage/cost snapshots: file import and read-only sync.")
app.add_typer(schema_app, name="schema")
app.add_typer(billing_app, name="billing")

WorkspaceOpt = Annotated[
    Path | None,
    typer.Option(
        "--workspace",
        "-w",
        help="Workspace directory (default: AIECON_WORKSPACE env or .aiecon/live)",
    ),
]
DatasetOpt = Annotated[
    str | None, typer.Option("--dataset-id", help="Dataset to use when the workspace holds several")
]


def _workspace(ctx: typer.Context) -> Path:
    return resolve_workspace((ctx.obj or {}).get("workspace"))


def _echo_kv(pairs: dict[str, Any]) -> None:
    width = max(len(k) for k in pairs) if pairs else 0
    for key, value in pairs.items():
        typer.echo(f"{key.ljust(width)}  {value}")


def _guard(fn: Callable[[], int]) -> None:
    """Map exceptions to the documented exit codes without leaking content."""

    try:
        code = fn()
    except WorkspaceError as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    except FileNotFoundError as exc:
        typer.echo(f"configuration error: file not found: {exc}", err=True)
        raise typer.Exit(EXIT_CONFIG) from None
    except (BillingImportError, CatalogError) as exc:
        typer.echo(f"data contract error: {exc}", err=True)
        raise typer.Exit(EXIT_CONTRACT) from None
    except ProviderReportError as exc:
        typer.echo(f"provider report error: {exc}", err=True)
        raise typer.Exit(EXIT_FAILURE) from None
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        typer.echo(f"error: {type(exc).__name__}", err=True)
        if debug_enabled():
            traceback.print_exc()
        else:
            typer.echo("set AIECON_DEBUG=1 for the traceback", err=True)
        raise typer.Exit(EXIT_FAILURE) from None
    raise typer.Exit(code)


@app.callback()
def main(ctx: typer.Context, workspace: WorkspaceOpt = None) -> None:
    ctx.obj = {"workspace": workspace}


@app.command()
def version() -> None:
    """Print the aiecon version."""

    typer.echo(f"aiecon {__version__}")


@app.command()
def init(
    ctx: typer.Context,
    data_kind: Annotated[
        DataKind, typer.Option("--data-kind", help="live (default) or synthetic")
    ] = DataKind.live,
) -> None:
    """Create the workspace directories, database schema and workspace manifest."""

    def run() -> int:
        from aiecon.pipeline import run_init

        path = _workspace(ctx)
        manifest = run_init(path, data_kind=data_kind, now_ms=now_ms())
        _echo_kv(
            {
                "workspace": str(path.resolve()),
                "workspace_id": manifest.workspace_id,
                "data_kind": manifest.data_kind.value,
                "schema_version": manifest.schema_version,
            }
        )
        return EXIT_OK

    _guard(run)


@app.command()
def doctor(
    ctx: typer.Context,
    mode: Annotated[str, typer.Option("--mode", help="offline or live")] = "offline",
) -> None:
    """Check the installation (offline) or the live configuration (no paid calls)."""

    def run() -> int:
        from aiecon.pipeline import run_doctor_offline

        if mode not in ("offline", "live"):
            raise WorkspaceError("--mode must be offline or live")
        checks = run_doctor_offline()
        if mode == "live":
            checks.extend(_live_checks(ctx))
        failed = 0
        for check in checks:
            mark = "ok  " if check.ok else "FAIL"
            typer.echo(f"[{mark}] {check.name}: {check.detail}")
            failed += 0 if check.ok else 1
        typer.echo(f"{len(checks) - failed}/{len(checks)} checks passed")
        return EXIT_OK if failed == 0 else EXIT_CONFIG

    _guard(run)


def _live_checks(ctx: typer.Context) -> list[Any]:
    from aiecon.pipeline import Check

    checks: list[Check] = []
    try:
        import litellm  # noqa: F401

        checks.append(Check("litellm", True, "installed (live extra)"))
    except ImportError:
        checks.append(Check("litellm", False, "not installed; run: uv sync --locked --extra live"))
    for name in MODEL_KEY_ENVS:
        present = env_present(name)
        checks.append(Check(f"env {name}", present, "present" if present else "missing"))
    for provider, name in ADMIN_KEY_ENVS.items():
        present = env_present(name)
        detail = (
            "present" if present else f"missing (billing sync --provider {provider} unavailable)"
        )
        checks.append(Check(f"env {name}", True, detail))
    for provider, name in LIVE_MODEL_ENVS.items():
        value = env_value_non_secret(name)
        checks.append(
            Check(f"env {name}", value is not None, value or f"missing ({provider} model id)")
        )
    fp = env_present(FINGERPRINT_KEY_ENV)
    detail = "present" if fp else "missing (prefix fingerprints disabled)"
    checks.append(Check(f"env {FINGERPRINT_KEY_ENV}", True, detail))
    path = _workspace(ctx)
    checks.append(Check("workspace", (path / "state" / "workspace.json").exists(), str(path)))
    return checks


@app.command()
def ingest(
    ctx: typer.Context,
    input: Annotated[
        list[Path], typer.Option("--input", help="JSONL file or directory (repeatable)")
    ],
) -> None:
    """Idempotently project allowlisted envelope JSONL into the workspace database."""

    def run() -> int:
        from aiecon.pipeline import run_ingest

        stats = run_ingest(_workspace(ctx), input, now_ms=now_ms())
        summary = stats.to_dict()
        skip = {"rejected_lines", "conflict_event_ids"}
        _echo_kv({k: v for k, v in summary.items() if k not in skip})
        for item in summary["rejected_lines"]:
            typer.echo(f"rejected  {item['file']}:{item['line']}  {item['error']}")
        for event_id in summary["conflict_event_ids"]:
            typer.echo(f"conflict  event_id={event_id} (original kept)")
        return EXIT_CONTRACT if stats.conflicts else EXIT_OK

    _guard(run)


@app.command()
def estimate(
    ctx: typer.Context,
    catalog: Annotated[Path, typer.Option("--catalog", help="Price catalog JSON file")],
    dataset_id: DatasetOpt = None,
) -> None:
    """Price every ingested call with the catalog and register the pricing run."""

    def run() -> int:
        from aiecon.pipeline import run_estimate

        result = run_estimate(_workspace(ctx), catalog, now_ms=now_ms(), dataset_id=dataset_id)
        m = result.manifest
        _echo_kv(
            {
                "pricing_run_id": m.pricing_run_id,
                "dataset_id": m.dataset_id,
                "catalog": f"{m.catalog_version} ({m.catalog_kind}, hash {m.catalog_hash[:12]})",
                "calls": m.call_count,
                "priced/partial/unpriced": (
                    f"{m.priced_call_count}/{m.partially_priced_call_count}/{m.unpriced_call_count}"
                ),
                "line_items": m.line_item_count,
                "known_cost_subtotal_usd": format(m.known_cost_subtotal_usd, "f"),
                "cost_complete": m.cost_complete,
                "replaced_existing_run": result.replaced_existing,
            }
        )
        return EXIT_OK

    _guard(run)


@billing_app.command("import")
def billing_import(
    ctx: typer.Context,
    file: Annotated[Path, typer.Option("--file", help="Normalized CSV or JSON records")],
    manifest: Annotated[Path, typer.Option("--manifest", help="Snapshot manifest JSON")],
) -> None:
    """Import a complete provider usage/cost snapshot with its provenance manifest."""

    def run() -> int:
        from aiecon.pipeline import run_billing_import

        result = run_billing_import(
            _workspace(ctx), file_path=file, manifest_path=manifest, now_ms=now_ms()
        )
        total = None if result.total_amount_usd is None else format(result.total_amount_usd, "f")
        _echo_kv(
            {
                "snapshot_id": result.snapshot_id,
                "records": result.record_count,
                "skipped_same_hash": result.skipped_same_hash,
                "superseded_snapshots": ", ".join(result.superseded_snapshot_ids) or "-",
                "non_usd_records": result.non_usd_records,
                "total_amount_usd": total if total is not None else "n/a (usage snapshot)",
            }
        )
        return EXIT_OK

    _guard(run)


@billing_app.command("sync")
def billing_sync(
    ctx: typer.Context,
    provider: Annotated[str, typer.Option("--provider", help="openai or anthropic")],
    start: Annotated[str, typer.Option("--start", help="UTC date (inclusive) or ISO datetime")],
    end: Annotated[str, typer.Option("--end", help="UTC date (exclusive) or ISO datetime")],
    scope_id: Annotated[
        str | None,
        typer.Option(
            "--scope-id", help="Local scope id for the pulled data (default: provider name)"
        ),
    ] = None,
    scope_filter: Annotated[
        list[str] | None,
        typer.Option(
            "--filter", help="Provider project/workspace id to restrict the pull (repeatable)"
        ),
    ] = None,
    dedicated_scope: Annotated[
        bool,
        typer.Option(
            "--dedicated-scope",
            help=(
                "Declare that the pulled project/workspace carries only the traffic captured "
                "locally, so a provider-side surplus counts as a capture gap (evidence) "
                "instead of a hypothesis"
            ),
        ),
    ] = False,
) -> None:
    """Pull complete usage and cost snapshots from the provider report API (read-only)."""

    def run() -> int:
        from aiecon.pipeline import run_billing_sync

        result = run_billing_sync(
            _workspace(ctx),
            provider=provider,
            start=start,
            end=end,
            scope_id=scope_id,
            scope_filter=scope_filter,
            dedicated_scope=dedicated_scope,
            now_ms=now_ms(),
        )
        _echo_kv(
            {
                "provider": result.provider.value,
                "scope_dedicated": dedicated_scope,
                "window_utc": f"{result.window.start_ms} .. {result.window.end_ms} (ms)",
                "snapshots": ", ".join(
                    f"{i.snapshot_id}:{i.record_count}"
                    + (" (unchanged)" if i.skipped_same_hash else "")
                    for i in result.imports
                ),
                "staged_under": str(result.staged_dir),
            }
        )
        return EXIT_OK

    _guard(run)


@app.command()
def reconcile(
    ctx: typer.Context,
    start: Annotated[str, typer.Option("--start", help="UTC date (inclusive) or ISO datetime")],
    end: Annotated[str, typer.Option("--end", help="UTC date (exclusive) or ISO datetime")],
    dataset_id: DatasetOpt = None,
) -> None:
    """Compare local usage and estimates with provider reports at the provider's grain."""

    def run() -> int:
        from aiecon.pipeline import run_reconcile

        result = run_reconcile(
            _workspace(ctx), start=start, end=end, dataset_id=dataset_id, now_ms=now_ms()
        )
        manifest = result.manifest
        typer.echo(f"reconcile_run_id {manifest.reconcile_run_id}  buckets {manifest.bucket_count}")
        header = (
            f"{'kind':18s} {'provider':10s} {'scope':24s} {'day':10s} {'model':28s} "
            f"{'E (usd)':>14s} {'B (usd)':>14s} {'variance':>12s} {'pct':>8s} status / reasons"
        )
        typer.echo(header)
        for b in result.buckets:
            e = "-" if b.local_estimate_usd is None else format(b.local_estimate_usd, ".6f")
            bv = "-" if b.provider_cost_usd is None else format(b.provider_cost_usd, ".6f")
            var = "-" if b.signed_variance_usd is None else format(b.signed_variance_usd, "+.6f")
            pct = "-" if b.variance_pct is None else format(b.variance_pct, "+.2f")
            if b.comparison_kind.value == "usage_comparison":
                e = bv = var = pct = "-"
            reasons = ", ".join(b.reasons)
            typer.echo(
                f"{b.comparison_kind.value:18s} {b.provider.value:10s} {b.scope_id[:24]:24s} "
                f"{b.dimensions.get('day', ''):10s} {(b.dimensions.get('model') or '')[:28]:28s} "
                f"{e:>14s} {bv:>14s} {var:>12s} {pct:>8s} {b.status.value}"
                + (f" / {reasons}" if reasons else "")
            )
        typer.echo("")
        for line in result.summary_lines:
            typer.echo(line)
        return EXIT_OK

    _guard(run)


@app.command()
def report(
    ctx: typer.Context,
    out: Annotated[
        Path, typer.Option("--out", help="HTML output path; JSON and manifest go next to it")
    ],
    monthly_requests: Annotated[
        int | None,
        typer.Option("--monthly-requests", help="Add a clearly labeled monthly projected scenario"),
    ] = None,
    dataset_id: DatasetOpt = None,
    allow_stale: Annotated[
        bool,
        typer.Option(
            "--allow-stale",
            help=(
                "Render even when the active pricing/reconcile runs no longer match the "
                "workspace (the report is then banner-marked STALE INPUTS)"
            ),
        ),
    ] = False,
) -> None:
    """Render the single-page HTML report plus JSON and manifest from the selected runs."""

    def run() -> int:
        from aiecon.pipeline import run_report

        paths, manifest = run_report(
            _workspace(ctx),
            out=out,
            now_ms=now_ms(),
            dataset_id=dataset_id,
            monthly_requests=monthly_requests,
            allow_stale=allow_stale,
        )
        _echo_kv(
            {
                "report_html": str(paths.html.resolve()),
                "report_json": str(paths.json.resolve()),
                "report_manifest": str(paths.manifest.resolve()),
                "data_kind": manifest.data_kind.value,
                "pricing_run_id": manifest.pricing_run_id or "-",
                "reconcile_run_id": manifest.reconcile_run_id or "-",
            }
        )
        if manifest.data_kind.value == "synthetic":
            typer.echo("SYNTHETIC DATA")
        return EXIT_OK

    _guard(run)


@app.command()
def demo(
    out: Annotated[Path, typer.Option("--out", help="Demo workspace directory")] = Path(
        ".aiecon/demo"
    ),
) -> None:
    """Run the offline synthetic demo end to end. No API key, no network."""

    def run() -> int:
        from aiecon.pipeline import DEMO_BANNER, run_demo

        result = run_demo(out, now_ms=now_ms())
        pricing = result.pricing.manifest if result.pricing else None
        _echo_kv(
            {
                "workspace": str(result.workspace.resolve()),
                "stages": ", ".join(result.stages),
                "events_accepted": result.ingest.accepted,
                "calls": f"{result.calls} (expected {result.expected_calls})",
                "outcomes": f"{result.outcomes} (expected {result.expected_outcomes})",
                "duplicates/conflicts/rejected": (
                    f"{result.ingest.duplicates}/{result.ingest.conflicts}/{result.ingest.rejected}"
                ),
                "pricing_run": pricing.pricing_run_id if pricing else "-",
                "known_cost_subtotal_usd": (
                    format(pricing.known_cost_subtotal_usd, "f") if pricing else "-"
                ),
                "unpriced_calls": pricing.unpriced_call_count if pricing else "-",
                "provider_snapshots": ", ".join(
                    f"{i.snapshot_id}:{i.record_count}" for i in result.imports
                ),
            }
        )
        if result.reconciliation is not None:
            typer.echo("")
            for line in result.reconciliation.summary_lines:
                typer.echo(line)
            typer.echo("")
        for name, path in result.outputs.items():
            typer.echo(f"{name}: {path.resolve()}")
        typer.echo(DEMO_BANNER)
        if not result.counts_match_expected:
            typer.echo("demo counts differ from expected_metrics.json", err=True)
            return EXIT_FAILURE
        return EXIT_OK

    _guard(run)


@schema_app.command("export")
def schema_export(
    out: Annotated[Path, typer.Option("--out", help="Output directory for *.schema.json")],
) -> None:
    """Write JSON Schema files for every data contract. Needs no database."""

    def run() -> int:
        from aiecon.spec.schema_export import export_json_schemas

        written = export_json_schemas(out)
        for path in written:
            typer.echo(str(path))
        typer.echo(f"{len(written)} schemas written to {out.resolve()}")
        return EXIT_OK

    _guard(run)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
