"""Data access: the only place that translates between ORM rows and domain models.

Routers and services call these functions; nothing else touches SQLAlchemy
directly. Each function takes an explicit ``Session`` so the caller controls the
transaction boundary -- the background worker and the HTTP layer have different
lifetimes and must not share one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from patchpilot_core.config import Settings, get_settings
from patchpilot_core.enums import JobStatus, JobType, RunState, RunStatus
from patchpilot_core.errors import NotFoundError
from patchpilot_core.ids import benchmark_id, job_id, new_id, repo_id, run_id, task_id
from patchpilot_core.models import (
    AttemptRecord,
    IssueSpec,
    RepositorySpec,
    RunConfig,
    StateTransition,
)
from patchpilot_indexer import RepositoryIndex
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from .orm import (
    Artifact,
    Attempt,
    BenchmarkResult,
    BenchmarkRun,
    CodeChunkRow,
    ImportEdgeRow,
    Issue,
    Job,
    Repository,
    RepositoryIndexRow,
    Run,
    RunEvent,
    SymbolRow,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Repositories and issues
# --------------------------------------------------------------------------- #
def upsert_repository(
    session: Session,
    spec: RepositorySpec,
    *,
    local_path: str | None = None,
    source: str = "git-clone",
) -> Repository:
    stmt = select(Repository).where(
        Repository.url == spec.url,
        Repository.branch == spec.branch,
    )
    row = session.scalars(stmt).first()
    if row is None:
        row = Repository(
            id=repo_id(),
            url=spec.url,
            slug=spec.slug,
            branch=spec.branch,
            commit_sha=spec.commit_sha,
            source=source,
            local_path=local_path,
        )
        session.add(row)
        session.flush()
    else:
        if spec.commit_sha:
            row.commit_sha = spec.commit_sha
        if local_path:
            row.local_path = local_path
    return row


def get_repository(session: Session, identifier: str) -> Repository:
    row = session.get(Repository, identifier)
    if row is None:
        raise NotFoundError(f"repository {identifier} was not found")
    return row


def list_repositories(session: Session, limit: int = 100) -> list[Repository]:
    return list(
        session.scalars(select(Repository).order_by(Repository.created_at.desc()).limit(limit))
    )


def create_issue(session: Session, repository: Repository, issue: IssueSpec) -> Issue:
    row = Issue(
        id=task_id(),
        repository_id=repository.id,
        source=issue.source,
        number=issue.number,
        title=issue.title,
        body=issue.body,
        url=issue.url,
        labels=list(issue.labels),
    )
    session.add(row)
    session.flush()
    return row


def get_issue(session: Session, identifier: str) -> Issue:
    row = session.get(Issue, identifier)
    if row is None:
        raise NotFoundError(f"issue {identifier} was not found")
    return row


def issue_to_spec(row: Issue) -> IssueSpec:
    return IssueSpec(
        source=row.source,  # type: ignore[arg-type]
        number=row.number,
        title=row.title,
        body=row.body,
        url=row.url,
        labels=list(row.labels or []),
    )


def repository_to_spec(row: Repository) -> RepositorySpec:
    return RepositorySpec(url=row.url, branch=row.branch, commit_sha=row.commit_sha, name=row.slug)


# --------------------------------------------------------------------------- #
# Index persistence
# --------------------------------------------------------------------------- #
def save_index(
    session: Session,
    repository: Repository,
    index: RepositoryIndex,
    *,
    embedding_provider: str,
    vector_store: str,
) -> RepositoryIndexRow:
    """Persist index metadata, replacing any previous index for the same SHA."""
    existing = session.scalars(
        select(RepositoryIndexRow).where(
            RepositoryIndexRow.repository_id == repository.id,
            RepositoryIndexRow.repo_sha == index.repo_sha,
        )
    ).first()
    if existing is not None:
        session.delete(existing)
        session.flush()

    row = RepositoryIndexRow(
        id=new_id("idx"),
        repository_id=repository.id,
        repo_sha=index.repo_sha,
        root_path=str(index.root),
        stats=index.stats.model_dump(mode="json"),
        conventions={name: body[:2000] for name, body in index.conventions.items()},
        embedding_provider=embedding_provider,
        vector_store=vector_store,
    )
    session.add(row)
    session.flush()

    session.add_all(
        CodeChunkRow(
            index_id=row.id,
            chunk_id=chunk.chunk_id,
            repo_sha=chunk.repo_sha,
            path=chunk.path,
            language=chunk.language,
            symbol=chunk.symbol,
            symbol_type=str(chunk.symbol_type),
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            is_test=chunk.is_test,
            token_estimate=chunk.token_estimate,
            docstring=chunk.docstring,
            content=chunk.content,
            imports=list(chunk.imports),
            calls=list(chunk.calls),
            calls_resolved=list(chunk.calls_resolved),
            called_by=list(chunk.called_by),
        )
        for chunk in index.chunks
    )
    session.add_all(
        ImportEdgeRow(
            index_id=row.id,
            source_path=edge.source_path,
            target_path=edge.target_path,
            module=edge.module,
            symbol=edge.symbol,
            line=edge.line,
            resolved=edge.resolved,
        )
        for edge in index.edges
    )
    session.add_all(
        SymbolRow(
            index_id=row.id,
            path=symbol.path,
            qualified_name=symbol.qualified_name,
            symbol_type=str(symbol.symbol_type),
            start_line=symbol.start_line,
            end_line=symbol.end_line,
        )
        for symbol in index.symbols
    )
    session.flush()
    return row


def get_index(session: Session, repository_id: str, repo_sha: str | None = None):
    stmt = select(RepositoryIndexRow).where(RepositoryIndexRow.repository_id == repository_id)
    if repo_sha:
        stmt = stmt.where(RepositoryIndexRow.repo_sha == repo_sha)
    return session.scalars(stmt.order_by(RepositoryIndexRow.created_at.desc())).first()


def index_symbols(session: Session, index_id: str, path: str | None = None) -> list[SymbolRow]:
    stmt = select(SymbolRow).where(SymbolRow.index_id == index_id)
    if path:
        stmt = stmt.where(SymbolRow.path == path)
    return list(session.scalars(stmt.order_by(SymbolRow.path, SymbolRow.start_line)))


def index_graph(session: Session, index_id: str) -> list[ImportEdgeRow]:
    return list(session.scalars(select(ImportEdgeRow).where(ImportEdgeRow.index_id == index_id)))


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
def create_run(
    session: Session,
    *,
    repository: Repository,
    issue: Issue,
    config: RunConfig,
    identifier: str | None = None,
) -> Run:
    row = Run(
        id=identifier or run_id(),
        repository_id=repository.id,
        issue_id=issue.id,
        model=config.model,
        status=str(RunStatus.QUEUED),
        state=str(RunState.INGEST),
        config=config.model_dump(mode="json"),
    )
    session.add(row)
    session.flush()
    return row


def get_run(session: Session, identifier: str) -> Run:
    row = session.get(Run, identifier)
    if row is None:
        raise NotFoundError(f"run {identifier} was not found")
    return row


def list_runs(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    model: str | None = None,
    repository_id: str | None = None,
) -> tuple[list[Run], int]:
    stmt = select(Run)
    count_stmt = select(func.count()).select_from(Run)
    if status:
        stmt = stmt.where(Run.status == status)
        count_stmt = count_stmt.where(Run.status == status)
    if model:
        stmt = stmt.where(Run.model == model)
        count_stmt = count_stmt.where(Run.model == model)
    if repository_id:
        stmt = stmt.where(Run.repository_id == repository_id)
        count_stmt = count_stmt.where(Run.repository_id == repository_id)
    total = session.scalar(count_stmt) or 0
    rows = list(session.scalars(stmt.order_by(Run.created_at.desc()).limit(limit).offset(offset)))
    return rows, total


def record_event(session: Session, run_identifier: str, transition: StateTransition) -> RunEvent:
    row = RunEvent(
        run_id=run_identifier,
        index=transition.index,
        from_state=str(transition.from_state) if transition.from_state else None,
        to_state=str(transition.to_state),
        reason=transition.reason,
        attempt=transition.attempt,
        duration_ms=transition.duration_ms,
        detail=transition.detail,
        at=transition.at,
    )
    session.add(row)
    return row


def next_event_index(session: Session, run_identifier: str) -> int:
    """The index a resumed run should continue its transition log from.

    Transitions are append-only and uniquely indexed per run, so a resumed run
    continues the numbering instead of colliding with the history it already has.
    """
    highest = session.scalar(
        select(func.max(RunEvent.index)).where(RunEvent.run_id == run_identifier)
    )
    return 0 if highest is None else int(highest) + 1


def list_events(session: Session, run_identifier: str, after: int = -1) -> list[RunEvent]:
    return list(
        session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run_identifier, RunEvent.index > after)
            .order_by(RunEvent.index)
        )
    )


def record_attempt(session: Session, run_identifier: str, record: AttemptRecord) -> Attempt:
    existing = session.scalars(
        select(Attempt).where(Attempt.run_id == run_identifier, Attempt.attempt == record.attempt)
    ).first()
    row = existing or Attempt(id=new_id("att"), run_id=run_identifier, attempt=record.attempt)
    row.plan = record.plan.model_dump(mode="json") if record.plan else None
    row.plan_error = record.plan_error
    row.diff = record.patch.diff if record.patch else None
    row.diff_hash = record.patch.normalized_hash() if record.patch else None
    row.validation = record.validation.model_dump(mode="json") if record.validation else None
    row.execution = record.execution.model_dump(mode="json") if record.execution else None
    row.analysis = record.analysis
    row.should_retry = record.should_retry
    row.succeeded = record.succeeded
    row.input_tokens = record.usage.input_tokens
    row.output_tokens = record.usage.output_tokens
    row.duration_ms = record.duration_ms
    if existing is None:
        session.add(row)
    session.flush()
    return row


def list_attempts(session: Session, run_identifier: str) -> list[Attempt]:
    return list(
        session.scalars(
            select(Attempt).where(Attempt.run_id == run_identifier).order_by(Attempt.attempt)
        )
    )


def update_run_progress(session: Session, run_identifier: str, state: Mapping[str, Any]) -> Run:
    """Persist the live graph state onto the run row after every transition."""
    row = get_run(session, run_identifier)
    status = state.get("status")
    row.status = str(status) if status else row.status
    run_state = state.get("state")
    if run_state:
        row.state = str(run_state)
    stop_reason = state.get("stop_reason")
    row.stop_reason = str(stop_reason) if stop_reason else None
    row.repo_sha = state.get("repo_sha") or row.repo_sha
    row.attempts_used = state.get("attempt", row.attempts_used)
    row.commands = state.get("commands") or row.commands
    row.sandbox_backend = state.get("sandbox_backend") or row.sandbox_backend
    row.sandbox_isolated = bool(state.get("sandbox_isolated", row.sandbox_isolated))
    row.error = state.get("error")
    row.baseline_reproduced = state.get("baseline_reproduced")

    usage = state.get("usage")
    if usage is not None:
        row.input_tokens = usage.input_tokens
        row.output_tokens = usage.output_tokens
    latency = state.get("latency")
    if latency is not None:
        row.latency = latency.model_dump(mode="json")
    baseline = state.get("baseline")
    if baseline is not None:
        row.baseline = baseline.model_dump(mode="json")
    context = state.get("context")
    if context is not None:
        row.context_package = context.model_dump(mode="json")
    if row.started_at is None and row.status == str(RunStatus.RUNNING):
        row.started_at = utcnow()
    if row.status in {str(status) for status in RunStatus if status.is_terminal}:
        row.finished_at = row.finished_at or utcnow()
    return row


def finalize_run(session: Session, run_identifier: str, summary) -> Run:
    row = get_run(session, run_identifier)
    row.status = str(summary.status)
    row.state = str(summary.state)
    row.stop_reason = str(summary.stop_reason) if summary.stop_reason else None
    row.repo_sha = summary.repo_sha
    row.attempts_used = summary.attempts_used
    row.input_tokens = summary.usage.input_tokens
    row.output_tokens = summary.usage.output_tokens
    if summary.cost is not None:
        row.cost_usd = summary.cost.total_usd
        row.cost_known = summary.cost.pricing_known
    row.latency = summary.latency.model_dump(mode="json") if summary.latency else {}
    row.final_patch = summary.final_patch.diff if summary.final_patch else None
    row.error = summary.error
    row.finished_at = summary.finished_at or utcnow()
    return row


def request_cancel(session: Session, run_identifier: str) -> Run:
    row = get_run(session, run_identifier)
    row.cancel_requested = True
    return row


def cancel_requested(session: Session, run_identifier: str) -> bool:
    row = session.get(Run, run_identifier)
    return bool(row and row.cancel_requested)


# --------------------------------------------------------------------------- #
# Artifacts
# --------------------------------------------------------------------------- #
def write_artifact(
    session: Session,
    run_identifier: str,
    *,
    attempt: int,
    kind: str,
    filename: str,
    content: str,
    media_type: str = "text/plain",
    settings: Settings | None = None,
) -> Artifact:
    """Persist a safe artifact (diff or sanitised log) outside the sandbox."""
    settings = settings or get_settings()
    directory = settings.artifact_root / run_identifier
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = filename.replace("/", "_").replace("\\", "_")
    path = directory / safe_name
    data = content.encode("utf-8")
    path.write_bytes(data)

    row = Artifact(
        id=new_id("art"),
        run_id=run_identifier,
        attempt=attempt,
        kind=kind,
        filename=safe_name,
        media_type=media_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        path=str(path),
    )
    session.add(row)
    session.flush()
    return row


def list_artifacts(session: Session, run_identifier: str) -> list[Artifact]:
    return list(
        session.scalars(
            select(Artifact)
            .where(Artifact.run_id == run_identifier)
            .order_by(Artifact.attempt, Artifact.created_at)
        )
    )


def get_artifact(session: Session, artifact_identifier: str) -> Artifact:
    row = session.get(Artifact, artifact_identifier)
    if row is None:
        raise NotFoundError(f"artifact {artifact_identifier} was not found")
    return row


def artifact_path(row: Artifact) -> Path:
    return Path(row.path)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
def enqueue_job(
    session: Session,
    job_type: JobType,
    payload: dict[str, Any],
    *,
    correlation_id: str | None = None,
) -> Job:
    row = Job(
        id=job_id(),
        type=str(job_type),
        status=str(JobStatus.QUEUED),
        payload=payload,
        correlation_id=correlation_id,
    )
    session.add(row)
    session.flush()
    return row


def claim_next_job(session: Session) -> Job | None:
    """Atomically claim one queued job.

    The claim is a conditional UPDATE guarded on ``status = 'queued'``, which is
    atomic on SQLite and PostgreSQL alike. If another worker won the race the
    update affects zero rows and we look at the next candidate, so two workers
    can never run the same job.
    """
    for _ in range(10):
        candidate = session.scalars(
            select(Job).where(Job.status == str(JobStatus.QUEUED)).order_by(Job.created_at).limit(1)
        ).first()
        if candidate is None:
            return None
        claimed = cast(
            "CursorResult[Any]",
            session.execute(
                update(Job)
                .where(Job.id == candidate.id, Job.status == str(JobStatus.QUEUED))
                .values(
                    status=str(JobStatus.RUNNING),
                    started_at=utcnow(),
                    attempts=Job.attempts + 1,
                )
            ),
        )
        if claimed.rowcount == 1:
            session.flush()
            session.refresh(candidate)
            return candidate
        session.expire(candidate)
    return None


def finish_job(
    session: Session,
    identifier: str,
    *,
    status: JobStatus,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> Job:
    row = session.get(Job, identifier)
    if row is None:
        raise NotFoundError(f"job {identifier} was not found")
    row.status = str(status)
    row.result = result
    row.error = error
    row.finished_at = utcnow()
    return row


def requeue_orphaned_jobs(session: Session, *, max_attempts: int = 3) -> list[Job]:
    """Recover jobs left RUNNING by a crashed or killed worker.

    The worker is the only thing that moves a job into RUNNING, so on startup any
    job still in that state belongs to a process that no longer exists. Jobs are
    requeued up to ``max_attempts`` times and then failed, so a job that
    reproducibly kills the worker cannot loop forever.
    """
    stranded = list(session.scalars(select(Job).where(Job.status == str(JobStatus.RUNNING))))
    requeued: list[Job] = []
    for job in stranded:
        if job.attempts >= max_attempts:
            job.status = str(JobStatus.FAILED)
            job.error = (
                f"abandoned after {job.attempts} interrupted attempt(s); the worker "
                "did not survive this job."
            )
            job.finished_at = utcnow()
            continue
        job.status = str(JobStatus.QUEUED)
        job.started_at = None
        requeued.append(job)
    session.flush()
    return requeued


def resumable_runs(session: Session) -> list[Run]:
    """Runs left mid-flight, which the worker can pick up again."""
    return list(
        session.scalars(
            select(Run).where(Run.status.in_([str(RunStatus.QUEUED), str(RunStatus.RUNNING)]))
        )
    )


def get_job(session: Session, identifier: str) -> Job:
    row = session.get(Job, identifier)
    if row is None:
        raise NotFoundError(f"job {identifier} was not found")
    return row


def list_jobs(session: Session, limit: int = 50) -> list[Job]:
    return list(session.scalars(select(Job).order_by(Job.created_at.desc()).limit(limit)))


# --------------------------------------------------------------------------- #
# Benchmarks
# --------------------------------------------------------------------------- #
def create_benchmark(
    session: Session, *, dataset: str, models: list[str], tags: list[str]
) -> BenchmarkRun:
    row = BenchmarkRun(
        id=benchmark_id(),
        dataset=dataset,
        models=models,
        tags=tags,
        status=str(JobStatus.QUEUED),
    )
    session.add(row)
    session.flush()
    return row


def get_benchmark(session: Session, identifier: str) -> BenchmarkRun:
    row = session.get(BenchmarkRun, identifier)
    if row is None:
        raise NotFoundError(f"benchmark run {identifier} was not found")
    return row


def list_benchmarks(session: Session, limit: int = 50) -> list[BenchmarkRun]:
    return list(
        session.scalars(select(BenchmarkRun).order_by(BenchmarkRun.created_at.desc()).limit(limit))
    )


def add_benchmark_result(
    session: Session, benchmark: BenchmarkRun, **fields: Any
) -> BenchmarkResult:
    row = BenchmarkResult(id=new_id("res"), benchmark_id=benchmark.id, **fields)
    session.add(row)
    session.flush()
    return row


def list_benchmark_results(session: Session, benchmark_identifier: str) -> list[BenchmarkResult]:
    return list(
        session.scalars(
            select(BenchmarkResult)
            .where(BenchmarkResult.benchmark_id == benchmark_identifier)
            .order_by(BenchmarkResult.model, BenchmarkResult.task_id)
        )
    )
