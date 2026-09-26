"""Data retention: keep the data directory and the database from growing forever.

Every run leaves artifacts on disk and rows in the database, every distinct
commit leaves a checkout in the repository cache, and an attempt interrupted by
a crash leaves its workspace -- a full copy of the repository -- behind. Nothing
removed any of it, so a long-lived server eventually ran out of disk.

Two tiers:

* **Debris** is always safe to remove: workspaces and staging directories that
  no live run can be using. The worker sweeps it at startup.
* **History** -- finished runs, benchmarks, jobs and idle checkouts older than a
  cutoff -- is removed only when asked: ``patchpilot cleanup --older-than DAYS``,
  or automatically when ``PATCHPILOT_RETENTION_DAYS`` is set.

Vector-store namespaces are deliberately left alone: an index cached in memory
can outlive its file, and retrieval would then silently lose its semantic
signal.
"""

from __future__ import annotations

import contextlib
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

from patchpilot_core.config import Settings, get_settings
from patchpilot_core.enums import JobStatus, RunStatus
from patchpilot_core.logging import get_logger
from sqlalchemy import func, or_, select

from . import store
from .db import session_scope
from .orm import BenchmarkRun, Issue, Job, Run

logger = get_logger(__name__, component="retention")

# An attempt refreshes its run's workspace directory whenever it starts, and no
# attempt runs this long, so an untouched directory this old belongs to nobody.
WORKSPACE_GRACE_SECONDS = 6 * 3600

ACTIVE_RUN_STATUSES = (str(RunStatus.QUEUED), str(RunStatus.RUNNING))
FINISHED_JOB_STATUSES = (
    str(JobStatus.SUCCEEDED),
    str(JobStatus.FAILED),
    str(JobStatus.CANCELLED),
)


@dataclass(slots=True)
class CleanupReport:
    dry_run: bool = False
    runs: int = 0
    benchmarks: int = 0
    jobs: int = 0
    checkouts: int = 0
    workspaces: int = 0
    staging: int = 0
    bytes_freed: int = 0

    def as_dict(self) -> dict[str, int | bool]:
        return asdict(self)


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _remove(path: Path, report: CleanupReport) -> bool:
    with contextlib.suppress(OSError):
        report.bytes_freed += _size(path)
    if report.dry_run:
        return True
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def _age_seconds(path: Path, now: float) -> float:
    try:
        return now - path.stat().st_mtime
    except OSError:
        return 0.0


def sweep_debris(
    settings: Settings | None = None,
    *,
    grace_seconds: float = WORKSPACE_GRACE_SECONDS,
    report: CleanupReport | None = None,
) -> CleanupReport:
    """Remove workspaces and staging directories that nothing can be using."""
    settings = settings or get_settings()
    report = report or CleanupReport()
    now = time.time()

    with session_scope(settings) as session:
        active = set(session.scalars(select(Run.id).where(Run.status.in_(ACTIVE_RUN_STATUSES))))

    if settings.workspace_root.is_dir():
        for directory in settings.workspace_root.iterdir():
            if not directory.is_dir() or directory.name in active:
                continue
            if _age_seconds(directory, now) < grace_seconds:
                continue
            if _remove(directory, report):
                report.workspaces += 1

    if settings.repo_cache_root.is_dir():
        for directory in settings.repo_cache_root.glob(".staging-*"):
            if _age_seconds(directory, now) >= grace_seconds and _remove(directory, report):
                report.staging += 1
    return report


def cleanup(
    settings: Settings | None = None,
    *,
    older_than_days: float,
    dry_run: bool = False,
    grace_seconds: float = WORKSPACE_GRACE_SECONDS,
) -> CleanupReport:
    """Delete finished history older than ``older_than_days``, plus all debris."""
    if older_than_days <= 0:
        raise ValueError("older_than_days must be positive")
    settings = settings or get_settings()
    report = CleanupReport(dry_run=dry_run)
    cutoff = store.utcnow() - timedelta(days=older_than_days)
    finished_at = func.coalesce(Run.finished_at, Run.created_at)

    with session_scope(settings) as session:
        runs = list(
            session.scalars(
                select(Run).where(Run.status.not_in(ACTIVE_RUN_STATUSES), finished_at < cutoff)
            )
        )
        for run in runs:
            artifacts = settings.artifact_root / run.id
            if artifacts.is_dir():
                _remove(artifacts, report)
            if not dry_run:
                issue_id = run.issue_id
                session.delete(run)
                session.flush()
                still_used = session.scalar(
                    select(func.count()).select_from(Run).where(Run.issue_id == issue_id)
                )
                if not still_used:
                    issue = session.get(Issue, issue_id)
                    if issue is not None:
                        session.delete(issue)
            report.runs += 1

        benchmarks = list(
            session.scalars(
                select(BenchmarkRun).where(
                    BenchmarkRun.status.in_(FINISHED_JOB_STATUSES),
                    func.coalesce(BenchmarkRun.finished_at, BenchmarkRun.created_at) < cutoff,
                )
            )
        )
        for benchmark in benchmarks:
            if not dry_run:
                session.delete(benchmark)
            report.benchmarks += 1

        jobs = list(
            session.scalars(
                select(Job).where(
                    Job.status.in_(FINISHED_JOB_STATUSES),
                    or_(Job.finished_at.is_(None), Job.finished_at < cutoff),
                    Job.created_at < cutoff,
                )
            )
        )
        for job in jobs:
            if not dry_run:
                session.delete(job)
            report.jobs += 1

        if dry_run:
            session.rollback()

    # Checkouts are touched whenever a run ingests them, so their age is the
    # time since last use. A pruned one is simply ingested again when needed.
    now = time.time()
    max_age = older_than_days * 86_400
    if settings.repo_cache_root.is_dir():
        for directory in settings.repo_cache_root.iterdir():
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            if _age_seconds(directory, now) >= max_age and _remove(directory, report):
                report.checkouts += 1

    sweep_debris(settings, grace_seconds=grace_seconds, report=report)
    logger.info("retention cleanup finished", extra=report.as_dict())
    return report
