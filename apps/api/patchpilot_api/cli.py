"""The ``patchpilot`` command line.

Everything the dashboard can do is reachable from here, which is what makes the
demo reproducible in CI and on a machine with no browser open.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import typer
from patchpilot_core.config import get_settings, reset_settings
from patchpilot_core.logging import configure_logging
from patchpilot_core.models import RunConfig, SandboxLimits

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="PatchPilot: autonomous repository-level bug-fix agent and evaluation platform.",
)
db_app = typer.Typer(no_args_is_help=True, help="Database migrations.")
app.add_typer(db_app, name="db")


def _echo(message: str, *, error: bool = False) -> None:
    typer.echo(message, err=error)


# --------------------------------------------------------------------------- #
# serve
# --------------------------------------------------------------------------- #
@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(8000, help="Bind port"),
    reload: bool = typer.Option(False, help="Reload on code changes (development only)"),
) -> None:
    """Run the API and the background worker."""
    import uvicorn

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    uvicorn.run(
        "patchpilot_api.main:app",
        host=host,
        port=port,
        reload=reload,
        log_config=None,
    )


# --------------------------------------------------------------------------- #
# db
# --------------------------------------------------------------------------- #
@db_app.command("upgrade")
def db_upgrade(revision: str = typer.Argument("head")) -> None:
    """Apply migrations."""
    from .migrations import current_revision, upgrade

    upgrade(get_settings(), revision)
    _echo(f"database is at revision {current_revision(get_settings())}")


@db_app.command("downgrade")
def db_downgrade(revision: str = typer.Argument("-1")) -> None:
    """Revert migrations."""
    from .migrations import current_revision, downgrade

    downgrade(get_settings(), revision)
    _echo(f"database is at revision {current_revision(get_settings())}")


@db_app.command("current")
def db_current() -> None:
    """Show the applied and latest revisions."""
    from .migrations import current_revision, head_revision

    settings = get_settings()
    _echo(f"applied: {current_revision(settings)}")
    _echo(f"head:    {head_revision(settings)}")


# --------------------------------------------------------------------------- #
# sandbox
# --------------------------------------------------------------------------- #
@app.command()
def sandbox() -> None:
    """Report which sandbox backend would be used, and the controls it applies."""
    from patchpilot_sandbox import select_sandbox

    selection = select_sandbox(get_settings())
    _echo(json.dumps(selection.describe(), indent=2))
    if not selection.isolated and selection.available:
        _echo(
            "\nWARNING: this backend provides NO isolation. Use it only with "
            "repositories you trust.",
            error=True,
        )


# --------------------------------------------------------------------------- #
# index
# --------------------------------------------------------------------------- #
@app.command()
def index(
    repository: str = typer.Argument(..., help="Repository URL or local path"),
    branch: str | None = typer.Option(None),
    commit: str | None = typer.Option(None, help="Pin to a commit SHA"),
    show_symbols: bool = typer.Option(False, help="Print the parsed symbols"),
) -> None:
    """Clone (or copy) a repository and build the structural index."""
    from patchpilot_agent import ingest_repository
    from patchpilot_core.models import RepositorySpec
    from patchpilot_indexer import RepositoryIndexer

    settings = get_settings()
    configure_logging(settings.log_level, json_output=False)
    ingested = ingest_repository(
        RepositorySpec(url=repository, branch=branch, commit_sha=commit), settings
    )
    built = RepositoryIndexer(settings).index(Path(ingested.path), ingested.repo_sha)
    _echo(json.dumps(built.stats.model_dump(mode="json"), indent=2))
    if show_symbols:
        for symbol in built.symbols:
            _echo(f"{symbol.path}:{symbol.start_line}-{symbol.end_line} {symbol.qualified_name}")


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
@app.command()
def run(
    repository: str = typer.Argument(..., help="Repository URL or local path"),
    issue_file: Path | None = typer.Option(None, help="File containing the issue text"),
    issue: str | None = typer.Option(None, help="Issue text (inline)"),
    issue_number: int | None = typer.Option(None, help="GitHub issue number"),
    model: str | None = typer.Option(None, help="provider:model, e.g. mock:deterministic"),
    attempts: int = typer.Option(3, help="Maximum repair attempts"),
    validation_command: str | None = typer.Option(None, help="Command that must pass"),
    backend: str = typer.Option("auto", help="Sandbox backend: auto, docker or local"),
    timeout: int = typer.Option(180, help="Per-command wall-clock limit in seconds"),
    json_output: bool = typer.Option(False, "--json", help="Print the result as JSON"),
) -> None:
    """Run the repair agent once, printing the state-machine timeline."""
    from patchpilot_agent import resolve_issue, run_agent
    from patchpilot_core.models import RepositorySpec

    settings = get_settings()
    configure_logging("WARNING", json_output=False)

    text = issue
    if issue_file is not None:
        text = issue_file.read_text(encoding="utf-8")
    issue_spec = resolve_issue(
        repository, issue_number=issue_number, issue_text=text, settings=settings
    )

    config = RunConfig(
        model=model or settings.default_model,
        max_repair_attempts=attempts,
        validation_command=validation_command,
        sandbox_backend=backend,  # type: ignore[arg-type]
        limits=SandboxLimits(timeout_seconds=timeout),
    )
    outcome = run_agent(
        repository=RepositorySpec(url=repository),
        issue=issue_spec,
        config=config,
        settings=settings,
    )

    if json_output:
        _echo(outcome.summary.model_dump_json(indent=2))
    else:
        _echo(f"\nrun {outcome.summary.run_id}")
        for transition in outcome.state.get("transitions", []):
            _echo(
                f"  {transition.from_state or '·':>16} -> {transition.to_state:<16} "
                f"{transition.duration_ms:>6}ms  "
                f"{textwrap.shorten(transition.reason, 90, placeholder='…')}"
            )
        _echo(f"\nstatus: {outcome.summary.status} ({outcome.summary.stop_reason})")
        _echo(f"attempts: {outcome.summary.attempts_used}")
        if outcome.summary.final_patch:
            _echo("\n" + outcome.summary.final_patch.diff)
    raise typer.Exit(code=0 if str(outcome.summary.status) == "fixed" else 1)


# --------------------------------------------------------------------------- #
# bench
# --------------------------------------------------------------------------- #
@app.command()
def bench(
    dataset: str = typer.Argument("patchpilot-fixtures", help="Dataset name or path"),
    models: list[str] = typer.Option(["mock:deterministic"], "--model", "-m", help="Repeatable"),
    tags: list[str] = typer.Option([], "--tag", help="Only tasks with these tags"),
    output: Path = typer.Option(
        Path("benchmark-output"), help="Directory for the JSON and Markdown reports"
    ),
) -> None:
    """Benchmark one or more models against the same task set."""
    from patchpilot_evals import (
        BenchmarkRunner,
        load_dataset,
        render_json_report,
        render_markdown_report,
    )
    from patchpilot_sandbox import select_sandbox

    settings = get_settings()
    configure_logging("WARNING", json_output=False)
    loaded = load_dataset(dataset)
    if tags:
        loaded = loaded.filter_by_tags(list(tags))
    selection = select_sandbox(settings)
    _echo(f"sandbox: {selection.backend} (isolated={selection.isolated})")
    if selection.available and not selection.isolated:
        _echo("  NOTE: no isolation available here; fixtures only.", error=True)
    _echo(f"dataset {loaded.name}: {len(loaded.tasks)} task(s) x {len(models)} model(s)")

    def progress(result) -> None:  # type: ignore[no-untyped-def]
        mark = "PASS" if result.passed else "FAIL"
        _echo(
            f"  [{mark}] {result.model:<24} {result.task_id:<28} "
            f"{result.latency_ms:>6}ms  attempts={result.attempts_used}"
        )

    report = BenchmarkRunner(settings).run(loaded, list(models), progress=progress)
    json_path = output / f"{loaded.name}.json"
    markdown_path = output / f"{loaded.name}.md"
    render_json_report(report, json_path)
    render_markdown_report(report, markdown_path)
    _echo(f"\nwrote {json_path}\nwrote {markdown_path}")
    for summary in report.summaries:
        _echo(
            f"  {summary.model:<24} pass {summary.passed}/{summary.tasks} "
            f"median {summary.median_latency_ms:.0f}ms "
            f"tokens {summary.total_tokens:,} "
            f"cost ${summary.estimated_cost_usd:.4f}"
        )


# --------------------------------------------------------------------------- #
# demo
# --------------------------------------------------------------------------- #
@app.command()
def demo(
    model: str = typer.Option("mock:deterministic", help="Model to use for the demo"),
) -> None:
    """End-to-end offline demo: queue runs for every fixture and report the results.

    Needs no API key, no GitHub token and no network. With Docker running it uses
    the Docker sandbox; without it, it falls back to the local backend and says so.
    The runs are stored like any other, so the dashboard can inspect them after.
    """
    from patchpilot_evals import load_dataset

    from . import store
    from .db import reset_engine, session_scope
    from .migrations import ensure_schema
    from .services import create_run
    from .worker import Worker

    settings = get_settings()
    configure_logging("WARNING", json_output=False)
    settings.ensure_dirs()
    reset_engine()
    ensure_schema(settings)

    from patchpilot_sandbox import select_sandbox

    selection = select_sandbox(settings)
    _echo(f"sandbox: {selection.backend} (isolated={selection.isolated})")
    if not selection.available:
        _echo(f"no usable sandbox: {selection.reason}", error=True)
        raise typer.Exit(code=1)
    if not selection.isolated:
        _echo("  NOTE: no isolation available here; fixtures only.", error=True)

    dataset = load_dataset("patchpilot-fixtures")
    worker = Worker(settings, concurrency=1)
    worker.start()
    run_ids: list[tuple[str, str]] = []
    try:
        for task in dataset.tasks:
            run_identifier = create_run(
                repository=task.repository_spec(dataset.base_dir),
                issue=task.issue_spec(),
                config=RunConfig(
                    model=model,
                    max_repair_attempts=settings.max_repair_attempts,
                    validation_command=task.validation_command,
                    baseline_command=task.baseline_command,
                    limits=SandboxLimits(
                        timeout_seconds=settings.sandbox_timeout_seconds,
                        memory_mb=settings.sandbox_memory_mb,
                    ),
                ),
                settings=settings,
            )
            run_ids.append((task.id, run_identifier))
            _echo(f"queued {task.id} -> {run_identifier}")

        _echo("\nwaiting for the worker...")
        if not worker.wait_for_idle(timeout=900):
            _echo("timed out waiting for the queue to drain", error=True)
            raise typer.Exit(code=1)
    finally:
        worker.stop()

    failures = 0
    _echo("")
    with session_scope(settings) as session:
        for task_id, run_identifier in run_ids:
            row = store.get_run(session, run_identifier)
            mark = "PASS" if row.status == "fixed" else "FAIL"
            if row.status != "fixed":
                failures += 1
            _echo(
                f"[{mark}] {task_id:<28} {row.status:<18} attempts={row.attempts_used} "
                f"sandbox={row.sandbox_backend} run={run_identifier}"
            )
    _echo(
        f"\nRuns are stored in {settings.data_dir}. To inspect them, start the API with "
        "`patchpilot serve` and open http://127.0.0.1:8000/docs, or the dashboard."
    )
    raise typer.Exit(code=1 if failures else 0)


@app.command()
def version() -> None:
    """Print the version."""
    from .version import __version__

    _echo(__version__)


def main() -> None:  # pragma: no cover - console script entry point
    reset_settings()
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
