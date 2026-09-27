"""aiecon command line (PLAN.md section 10). Parsing and exit codes only; no business math."""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from aiecon import __version__
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
        checks.append(
            Check(f"env {name}", env_present(name), "present" if env_present(name) else "missing")
        )
    for provider, name in ADMIN_KEY_ENVS.items():
        present = env_present(name)
        checks.append(
            Check(
                f"env {name}",
                True,
                "present"
                if present
                else f"missing (billing sync --provider {provider} unavailable)",
            )
        )
    for provider, name in LIVE_MODEL_ENVS.items():
        value = env_value_non_secret(name)
        checks.append(
            Check(f"env {name}", value is not None, value or f"missing ({provider} model id)")
        )
    fp = env_present(FINGERPRINT_KEY_ENV)
    checks.append(
        Check(
            f"env {FINGERPRINT_KEY_ENV}",
            True,
            "present" if fp else "missing (prefix fingerprints disabled)",
        )
    )
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
        _echo_kv(
            {k: v for k, v in summary.items() if k not in ("rejected_lines", "conflict_event_ids")}
        )
        for item in summary["rejected_lines"]:
            typer.echo(f"rejected  {item['file']}:{item['line']}  {item['error']}")
        for event_id in summary["conflict_event_ids"]:
            typer.echo(f"conflict  event_id={event_id} (original kept)")
        return EXIT_CONTRACT if stats.conflicts else EXIT_OK

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
            }
        )
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


def _print_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
