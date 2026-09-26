"""Data access: the only place that translates between ORM rows and domain models.

Routers and services call these functions; nothing else touches SQLAlchemy
directly. Each function takes an explicit ``Session`` so the caller controls the
transaction boundary -- the background worker and the HTTP layer have different
lifetimes and must not share one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from patchpilot_core.config import Settings, get_settings
from patchpilot_core.enums import JobStatus, JobType, RunState, RunStatus, StopReason
from patchpilot_core.errors import NotFoundError, QueueFullError
from patchpilot_core.ids import benchmark_id, job_id, new_id, repo_id, run_id, task_id
from patchpilot_core.models import (
    AttemptRecord,
    IssueSpec,
    RepositorySpec,
    RunConfig,
    StateTransition,
)
from patchpilot_indexer import RepositoryIndex
from sqlalchemy import and_, delete, func, or_, select, update
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
    WorkerRow,
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


def count_jobs(session: Session, *statuses: JobStatus) -> int:
    stmt = select(func.count()).select_from(Job)
    if statuses:
        stmt = stmt.where(Job.status.in_([str(status) for status in statuses]))
    return int(session.scalar(stmt) or 0)


def ensure_queue_capacity(session: Session, settings: Settings | None = None) -> None:
    """Refuse new work while the queue is full, rather than letting it grow unbounded.

    A queue that only ever grows turns a burst of requests into hours of latency
    for everyone, and into a backlog that outlives the requests' usefulness.
    """
    settings = settings or get_settings()
    waiting = count_jobs(session, JobStatus.QUEUED)
    if waiting >= settings.max_queued_jobs:
        raise QueueFullError(
            f"the job queue is full ({waiting} waiting)",
            remediation="Retry once queued work has drained, or raise PATCHPILOT_MAX_QUEUED_JOBS.",
            context={"queued": waiting, "limit": settings.max_queued_jobs},
        )


def claim_next_job(session: Session, worker_id: str | None = None) -> Job | None:
    """Atomically claim one queued job and take its lease.

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
                    heartbeat_at=utcnow(),
                    worker_id=worker_id,
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
    worker_id: str | None = None,
) -> bool:
    """Record a job's outcome. Returns ``False`` if the lease was lost.

    With ``worker_id`` the write only lands while that worker still holds the
    job: a worker that stalled past its lease, and whose job was handed to
    another worker, must not overwrite the new owner's record.
    """
    row = session.get(Job, identifier)
    if row is None:
        raise NotFoundError(f"job {identifier} was not found")
    stmt = update(Job).where(Job.id == identifier)
    if worker_id is not None:
        stmt = stmt.where(Job.worker_id == worker_id, Job.status == str(JobStatus.RUNNING))
    written = cast(
        "CursorResult[Any]",
        session.execute(
            stmt.values(status=str(status), result=result, error=error, finished_at=utcnow()),
            execution_options={"synchronize_session": False},
        ),
    )
    session.expire(row)
    return written.rowcount == 1


def heartbeat_jobs(session: Session, job_ids: list[str], worker_id: str) -> int:
    """Renew the lease on jobs this worker is running. Returns how many it still holds."""
    if not job_ids:
        return 0
    renewed = cast(
        "CursorResult[Any]",
        session.execute(
            update(Job)
            .where(
                Job.id.in_(job_ids),
                Job.worker_id == worker_id,
                Job.status == str(JobStatus.RUNNING),
            )
            .values(heartbeat_at=utcnow()),
            execution_options={"synchronize_session": False},
        ),
    )
    return int(renewed.rowcount or 0)


@dataclass(slots=True)
class OrphanRecovery:
    requeued: list[Job] = field(default_factory=list)
    abandoned: list[Job] = field(default_factory=list)


def requeue_orphaned_jobs(
    session: Session, *, lease_seconds: float, max_attempts: int = 3
) -> OrphanRecovery:
    """Recover jobs whose worker died: RUNNING, with a heartbeat older than the lease.

    Only a stale lease makes a job an orphan. Treating every RUNNING job as one
    -- as a single-process worker could -- would, with a second process,
    requeue jobs that a live worker is still executing, and run them twice.

    Each transition is a conditional UPDATE re-checking the stale lease, so two
    workers sweeping at once cannot both act on a job, and a job claimed between
    the read and the write is left alone. Jobs are requeued up to
    ``max_attempts`` times and then abandoned, so a job that reproducibly kills
    its worker cannot loop forever.
    """
    cutoff = utcnow() - timedelta(seconds=lease_seconds)
    stale = and_(
        Job.status == str(JobStatus.RUNNING),
        or_(Job.heartbeat_at.is_(None), Job.heartbeat_at < cutoff),
    )
    recovery = OrphanRecovery()
    for job in list(session.scalars(select(Job).where(stale))):
        abandon = job.attempts >= max_attempts
        values: dict[str, Any] = (
            {
                "status": str(JobStatus.FAILED),
                "error": (
                    f"abandoned after {job.attempts} interrupted attempt(s); the worker "
                    "did not survive this job."
                ),
                "finished_at": utcnow(),
            }
            if abandon
            else {
                "status": str(JobStatus.QUEUED),
                "started_at": None,
                "heartbeat_at": None,
                "worker_id": None,
            }
        )
        changed = cast(
            "CursorResult[Any]",
            # No in-Python re-evaluation of the criteria: SQLite returns naive
            # datetimes, which cannot be compared with the aware cutoff.
            session.execute(
                update(Job).where(Job.id == job.id, stale).values(**values),
                execution_options={"synchronize_session": False},
            ),
        )
        if changed.rowcount != 1:
            continue
        session.expire(job)
        (recovery.abandoned if abandon else recovery.requeued).append(job)
    session.flush()
    return recovery


def active_jobs_for(session: Session, job_type: JobType, key: str, value: str) -> list[Job]:
    """Queued or running jobs of ``job_type`` whose payload has ``key == value``.

    Filtered in Python rather than with a JSON path expression, which is spelled
    differently on every database; the active set is bounded by the queue limit.
    """
    active = session.scalars(
        select(Job).where(
            Job.type == str(job_type),
            Job.status.in_([str(JobStatus.QUEUED), str(JobStatus.RUNNING)]),
        )
    )
    return [job for job in active if (job.payload or {}).get(key) == value]


def cancel_queued_job(session: Session, identifier: str) -> bool:
    """Cancel a job that no worker has claimed yet. ``False`` if one already has."""
    cancelled = cast(
        "CursorResult[Any]",
        session.execute(
            update(Job)
            .where(Job.id == identifier, Job.status == str(JobStatus.QUEUED))
            .values(status=str(JobStatus.CANCELLED), finished_at=utcnow()),
            execution_options={"synchronize_session": False},
        ),
    )
    job = session.get(Job, identifier)
    if job is not None:
        session.expire(job)
    return cancelled.rowcount == 1


def close_run(
    session: Session,
    run_identifier: str,
    *,
    status: RunStatus,
    stop_reason: StopReason,
    error: str | None = None,
) -> Run | None:
    """Put a run that never reached the end of its state machine into a terminal state.

    Used when the work around the graph fails -- a crashed job, an abandoned one,
    a cancellation before the run started -- so the run is not left showing
    "running" forever. A run that is already terminal is left untouched.
    """
    row = session.get(Run, run_identifier)
    if row is None or RunStatus(row.status).is_terminal:
        return None
    row.status = str(status)
    row.state = str(RunState.FINISHED)
    row.stop_reason = str(stop_reason)
    if error is not None:
        row.error = error
    row.finished_at = utcnow()
    return row


# --------------------------------------------------------------------------- #
# Worker registry
# --------------------------------------------------------------------------- #
def register_worker(
    session: Session,
    identifier: str,
    *,
    hostname: str,
    pid: int,
    concurrency: int,
    sandbox: dict[str, Any],
) -> WorkerRow:
    row = session.get(WorkerRow, identifier)
    if row is None:
        row = WorkerRow(id=identifier, hostname=hostname, pid=pid, started_at=utcnow())
        session.add(row)
    row.concurrency = concurrency
    row.sandbox = sandbox
    row.heartbeat_at = utcnow()
    # Rows of workers that vanished without deregistering, long past any lease.
    session.execute(
        delete(WorkerRow).where(WorkerRow.heartbeat_at < utcnow() - timedelta(days=1)),
        execution_options={"synchronize_session": False},
    )
    session.flush()
    return row


def heartbeat_worker(
    session: Session, identifier: str, *, sandbox: dict[str, Any] | None = None
) -> None:
    values: dict[str, Any] = {"heartbeat_at": utcnow()}
    if sandbox is not None:
        values["sandbox"] = sandbox
    session.execute(
        update(WorkerRow).where(WorkerRow.id == identifier).values(**values),
        execution_options={"synchronize_session": False},
    )


def deregister_worker(session: Session, identifier: str) -> None:
    session.execute(
        delete(WorkerRow).where(WorkerRow.id == identifier),
        execution_options={"synchronize_session": False},
    )


def live_workers(session: Session, *, lease_seconds: float) -> list[WorkerRow]:
    """Workers that have reported within the lease, freshest first."""
    cutoff = utcnow() - timedelta(seconds=lease_seconds)
    return list(
        session.scalars(
            select(WorkerRow)
            .where(WorkerRow.heartbeat_at >= cutoff)
            .order_by(WorkerRow.heartbeat_at.desc())
        )
    )


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
