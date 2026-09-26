"""Application services: the glue between HTTP, the queue and the agent.

Everything long-running happens here, called from the worker rather than from a
request handler. Each service opens its own short transactions so a run that
takes minutes never holds a database lock, and so a crash leaves the persisted
history consistent up to the last completed transition.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from patchpilot_agent import AgentState, RunObserver, ingest_repository, run_agent
from patchpilot_core.config import Settings, get_settings
from patchpilot_core.costs import estimate_cost
from patchpilot_core.enums import JobStatus, JobType, RunStatus
from patchpilot_core.errors import PatchPilotError
from patchpilot_core.logging import get_logger, log_context, new_correlation_id
from patchpilot_core.models import (
    AttemptRecord,
    IssueSpec,
    RepositorySpec,
    RunConfig,
    StateTransition,
)
from patchpilot_indexer import IndexCache, RepositoryIndexer

from . import store
from .db import session_scope

logger = get_logger(__name__, component="services")

# Shared by every worker thread; bounded so a long-lived server does not keep
# every index it has ever built.
_INDEX_CACHE = IndexCache(maxsize=16)


def _indexer(settings: Settings) -> RepositoryIndexer:
    return RepositoryIndexer(settings)


# --------------------------------------------------------------------------- #
# Run creation
# --------------------------------------------------------------------------- #
def create_run(
    *,
    repository: RepositorySpec,
    issue: IssueSpec,
    config: RunConfig,
    settings: Settings | None = None,
) -> str:
    """Persist a run and queue it. Returns the run id immediately."""
    settings = settings or get_settings()
    with session_scope(settings) as session:
        store.ensure_queue_capacity(session, settings)
        repository_row = store.upsert_repository(session, repository)
        issue_row = store.create_issue(session, repository_row, issue)
        run_row = store.create_run(
            session, repository=repository_row, issue=issue_row, config=config
        )
        store.enqueue_job(
            session,
            JobType.AGENT_RUN,
            {"run_id": run_row.id},
            correlation_id=new_correlation_id(),
        )
        return run_row.id


# --------------------------------------------------------------------------- #
# Run execution
# --------------------------------------------------------------------------- #
def _build_observer(run_identifier: str, settings: Settings) -> RunObserver:
    def on_transition(transition: StateTransition) -> None:
        with session_scope(settings) as session:
            store.record_event(session, run_identifier, transition)

    def on_attempt(record: AttemptRecord) -> None:
        with session_scope(settings) as session:
            store.record_attempt(session, run_identifier, record)
            _persist_attempt_artifacts(session, run_identifier, record, settings)

    def on_state(state: AgentState) -> None:
        with session_scope(settings) as session:
            store.update_run_progress(session, run_identifier, state)

    def should_cancel() -> bool:
        with session_scope(settings) as session:
            return store.cancel_requested(session, run_identifier)

    return RunObserver(
        on_transition=on_transition,
        on_attempt=on_attempt,
        on_state=on_state,
        should_cancel=should_cancel,
    )


def _persist_attempt_artifacts(
    session, run_identifier: str, record: AttemptRecord, settings: Settings
) -> None:
    """Keep the diff and the sanitised command output after the sandbox is gone."""
    if record.patch and record.patch.diff:
        store.write_artifact(
            session,
            run_identifier,
            attempt=record.attempt,
            kind="patch",
            filename=f"attempt-{record.attempt}.diff",
            content=record.patch.diff,
            media_type="text/x-diff",
            settings=settings,
        )
    if record.execution is not None:
        lines: list[str] = [
            f"# sandbox backend: {record.execution.backend}",
            f"# status: {record.execution.status}",
            f"# duration_ms: {record.execution.duration_ms}",
            "",
        ]
        for result in record.execution.results:
            lines.append(f"$ {result.command}")
            lines.append(
                f"# kind={result.kind} exit={result.exit_code} "
                f"status={result.status} duration_ms={result.duration_ms} "
                f"truncated={result.truncated}"
            )
            if result.stdout:
                lines.append(result.stdout)
            if result.stderr:
                lines.append("--- stderr ---")
                lines.append(result.stderr)
            lines.append("")
        store.write_artifact(
            session,
            run_identifier,
            attempt=record.attempt,
            kind="sandbox-log",
            filename=f"attempt-{record.attempt}.log",
            content="\n".join(lines),
            settings=settings,
        )


def execute_run(run_identifier: str, settings: Settings | None = None) -> dict[str, Any]:
    """Run the agent for a persisted run. Called by the worker."""
    settings = settings or get_settings()
    with session_scope(settings) as session:
        run_row = store.get_run(session, run_identifier)
        if run_row.cancel_requested:
            run_row.status = str(RunStatus.CANCELLED)
            return {"run_id": run_identifier, "status": str(RunStatus.CANCELLED)}
        repository = store.repository_to_spec(store.get_repository(session, run_row.repository_id))
        issue = store.issue_to_spec(store.get_issue(session, run_row.issue_id))
        config = RunConfig.model_validate(run_row.config)
        run_row.status = str(RunStatus.RUNNING)
        # Zero for a fresh run; for a run the worker was killed part-way through,
        # this continues the persisted transition log instead of colliding with it.
        transition_offset = store.next_event_index(session, run_identifier)

    with log_context(run_id=run_identifier, model=config.model):
        started = time.perf_counter()
        outcome = run_agent(
            repository=repository,
            issue=issue,
            config=config,
            settings=settings,
            run_id=run_identifier,
            observer=_build_observer(run_identifier, settings),
            indexer=_indexer(settings),
            index_cache=_INDEX_CACHE,
            transition_offset=transition_offset,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)

    with session_scope(settings) as session:
        store.finalize_run(session, run_identifier, outcome.summary)
        if outcome.summary.final_patch:
            store.write_artifact(
                session,
                run_identifier,
                attempt=outcome.summary.attempts_used,
                kind="final-patch",
                filename="final.diff",
                content=outcome.summary.final_patch.diff,
                media_type="text/x-diff",
                settings=settings,
            )
        # Persist the index for this SHA so the UI can browse symbols and the graph.
        index = _INDEX_CACHE.get(outcome.summary.repo_sha or "")
        if index is not None:
            run_row = store.get_run(session, run_identifier)
            repository_row = store.get_repository(session, run_row.repository_id)
            repository_row.commit_sha = outcome.summary.repo_sha
            repository_row.local_path = str(index.root)
            store.save_index(
                session,
                repository_row,
                index,
                embedding_provider=settings.embedding_provider,
                vector_store="qdrant" if settings.qdrant_url else "local",
            )

    logger.info(
        "run finished",
        extra={
            "run_id": run_identifier,
            "status": str(outcome.summary.status),
            "stop_reason": str(outcome.summary.stop_reason),
            "attempts": outcome.summary.attempts_used,
            "elapsed_ms": elapsed_ms,
        },
    )
    return {
        "run_id": run_identifier,
        "status": str(outcome.summary.status),
        "stop_reason": str(outcome.summary.stop_reason) if outcome.summary.stop_reason else None,
        "attempts_used": outcome.summary.attempts_used,
        "elapsed_ms": elapsed_ms,
    }


# --------------------------------------------------------------------------- #
# Standalone indexing
# --------------------------------------------------------------------------- #
def index_repository(
    repository_identifier: str, settings: Settings | None = None
) -> dict[str, Any]:
    """Clone (if needed) and index a repository without running the agent."""
    settings = settings or get_settings()
    with session_scope(settings) as session:
        repository_row = store.get_repository(session, repository_identifier)
        spec = store.repository_to_spec(repository_row)

    ingested = ingest_repository(spec, settings)
    indexer = _indexer(settings)
    index = indexer.index(Path(ingested.path), ingested.repo_sha)
    _INDEX_CACHE[ingested.repo_sha] = index

    with session_scope(settings) as session:
        repository_row = store.get_repository(session, repository_identifier)
        repository_row.commit_sha = ingested.repo_sha
        repository_row.local_path = str(ingested.path)
        repository_row.source = ingested.source
        row = store.save_index(
            session,
            repository_row,
            index,
            embedding_provider=settings.embedding_provider,
            vector_store="qdrant" if settings.qdrant_url else "local",
        )
        return {
            "repository_id": repository_identifier,
            "index_id": row.id,
            "repo_sha": ingested.repo_sha,
            "stats": index.stats.model_dump(mode="json"),
        }


# --------------------------------------------------------------------------- #
# Benchmarks
# --------------------------------------------------------------------------- #
def execute_benchmark(
    benchmark_identifier: str, settings: Settings | None = None
) -> dict[str, Any]:
    """Run a benchmark dataset against one or more models."""
    from patchpilot_evals import BenchmarkRunner, load_dataset, render_markdown_report

    settings = settings or get_settings()
    with session_scope(settings) as session:
        benchmark = store.get_benchmark(session, benchmark_identifier)
        dataset_path = benchmark.dataset
        models = list(benchmark.models)
        tags = list(benchmark.tags or [])
        benchmark.status = str(JobStatus.RUNNING)

    try:
        dataset = load_dataset(Path(dataset_path))
        if tags:
            dataset = dataset.filter_by_tags(tags)
        runner = BenchmarkRunner(settings=settings, index_cache=_INDEX_CACHE)
        report = runner.run(dataset, models, benchmark_id=benchmark_identifier)
    except Exception as exc:
        # Any failure, not only a domain error, must leave the benchmark in a
        # terminal state; otherwise the dashboard shows it "running" forever.
        if isinstance(exc, PatchPilotError):
            message = exc.message
        else:
            message = f"{type(exc).__name__}: {exc}"
        with session_scope(settings) as session:
            benchmark = store.get_benchmark(session, benchmark_identifier)
            benchmark.status = str(JobStatus.FAILED)
            benchmark.error = message
            benchmark.finished_at = store.utcnow()
        raise

    with session_scope(settings) as session:
        benchmark = store.get_benchmark(session, benchmark_identifier)
        for result in report.results:
            cost = estimate_cost(result.model, result.usage)
            store.add_benchmark_result(
                session,
                benchmark,
                task_id=result.task_id,
                model=result.model,
                run_id=result.run_id,
                status=str(result.status),
                passed=result.passed,
                baseline_failed=result.baseline_failed,
                patch_applied=result.patch_applied,
                attempts_used=result.attempts_used,
                latency_ms=result.latency_ms,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cost_usd=cost.total_usd,
                sandbox_failed=result.sandbox_failed,
                similarity=result.similarity,
                detail=result.detail,
                tags=result.tags,
            )
        benchmark.status = str(JobStatus.SUCCEEDED)
        benchmark.report = report.to_dict()
        benchmark.markdown = render_markdown_report(report)
        benchmark.finished_at = store.utcnow()

    return {"benchmark_id": benchmark_identifier, "tasks": len(report.results)}


# --------------------------------------------------------------------------- #
# Job dispatch
# --------------------------------------------------------------------------- #
JOB_HANDLERS = {
    str(JobType.AGENT_RUN): lambda payload, settings: execute_run(payload["run_id"], settings),
    str(JobType.INDEX_REPOSITORY): lambda payload, settings: index_repository(
        payload["repository_id"], settings
    ),
    str(JobType.BENCHMARK_RUN): lambda payload, settings: execute_benchmark(
        payload["benchmark_id"], settings
    ),
}
