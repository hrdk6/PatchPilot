"""Retention: history older than the cutoff goes, live work and recent history stay."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from patchpilot_api import store
from patchpilot_api.cli import app as cli
from patchpilot_api.db import reset_engine, session_scope
from patchpilot_api.migrations import ensure_schema
from patchpilot_api.orm import Artifact, Issue, RunEvent
from patchpilot_api.retention import cleanup, sweep_debris
from patchpilot_api.worker import Worker
from patchpilot_core.config import Settings, get_settings
from patchpilot_core.enums import JobStatus, JobType, RunState, RunStatus
from patchpilot_core.models import IssueSpec, RepositorySpec, RunConfig, StateTransition
from sqlalchemy import func, select
from typer.testing import CliRunner

DAY = 86_400


@pytest.fixture
def settings_db() -> Iterator[Settings]:
    reset_engine()
    settings = get_settings()
    settings.ensure_dirs()
    ensure_schema(settings)
    yield settings
    reset_engine()


def make_run(settings: Settings, *, status: RunStatus, age_days: float) -> str:
    with session_scope(settings) as session:
        repository = store.upsert_repository(session, RepositorySpec(url="https://x.test/r"))
        issue = store.create_issue(session, repository, IssueSpec(title="bug"))
        run = store.create_run(session, repository=repository, issue=issue, config=RunConfig())
        run.status = str(status)
        stamp = store.utcnow() - timedelta(days=age_days)
        run.created_at = stamp
        run.finished_at = stamp if status.is_terminal else None
        store.record_event(
            session,
            run.id,
            StateTransition(index=0, from_state=None, to_state=RunState.INGEST, reason="x"),
        )
        store.write_artifact(
            session,
            run.id,
            attempt=1,
            kind="patch",
            filename="attempt-1.diff",
            content="diff",
            settings=settings,
        )
        return run.id


def age(path: Path, days: float) -> None:
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))


def run_ids(settings: Settings) -> set[str]:
    with session_scope(settings) as session:
        return {run.id for run in store.list_runs(session, limit=200)[0]}


class TestHistory:
    def test_old_finished_runs_go_with_everything_they_own(self, settings_db: Settings) -> None:
        old = make_run(settings_db, status=RunStatus.FIXED, age_days=40)
        recent = make_run(settings_db, status=RunStatus.FIXED, age_days=1)

        report = cleanup(settings_db, older_than_days=30)

        assert report.runs == 1
        assert run_ids(settings_db) == {recent}
        assert not (settings_db.artifact_root / old).exists()
        assert (settings_db.artifact_root / recent).is_dir()
        with session_scope(settings_db) as session:
            for model, column in ((RunEvent, RunEvent.run_id), (Artifact, Artifact.run_id)):
                count = session.scalar(select(func.count()).select_from(model).where(column == old))
                assert count == 0
            assert session.scalar(select(func.count()).select_from(Issue)) == 1

    def test_live_runs_are_never_touched_however_old(self, settings_db: Settings) -> None:
        queued = make_run(settings_db, status=RunStatus.QUEUED, age_days=400)
        running = make_run(settings_db, status=RunStatus.RUNNING, age_days=400)
        cleanup(settings_db, older_than_days=1)
        assert run_ids(settings_db) == {queued, running}

    def test_a_dry_run_reports_and_deletes_nothing(self, settings_db: Settings) -> None:
        old = make_run(settings_db, status=RunStatus.ERROR, age_days=40)
        report = cleanup(settings_db, older_than_days=30, dry_run=True)
        assert report.runs == 1
        assert report.bytes_freed > 0
        assert run_ids(settings_db) == {old}
        assert (settings_db.artifact_root / old).is_dir()

    def test_old_benchmarks_and_finished_jobs_go_queued_jobs_stay(
        self, settings_db: Settings
    ) -> None:
        with session_scope(settings_db) as session:
            benchmark = store.create_benchmark(session, dataset="d", models=["m"], tags=[])
            benchmark.status = str(JobStatus.SUCCEEDED)
            benchmark.created_at = benchmark.finished_at = store.utcnow() - timedelta(days=40)
            finished = store.enqueue_job(session, JobType.AGENT_RUN, {"run_id": "a"})
            finished.status = str(JobStatus.SUCCEEDED)
            finished.created_at = finished.finished_at = store.utcnow() - timedelta(days=40)
            waiting = store.enqueue_job(session, JobType.AGENT_RUN, {"run_id": "b"})
            waiting.created_at = store.utcnow() - timedelta(days=40)
            waiting_id = waiting.id

        report = cleanup(settings_db, older_than_days=30)
        assert (report.benchmarks, report.jobs) == (1, 1)
        with session_scope(settings_db) as session:
            assert [job.id for job in store.list_jobs(session)] == [waiting_id]
            assert store.list_benchmarks(session) == []

    def test_idle_checkouts_are_pruned_recent_ones_kept(self, settings_db: Settings) -> None:
        idle = settings_db.repo_cache_root / "repo-aaaa"
        fresh = settings_db.repo_cache_root / "repo-bbbb"
        for directory in (idle, fresh):
            (directory / "src").mkdir(parents=True)
            (directory / "src" / "main.py").write_text("x = 1\n")
        age(idle, 40)

        report = cleanup(settings_db, older_than_days=30)
        assert report.checkouts == 1
        assert not idle.exists()
        assert fresh.is_dir()

    def test_the_cutoff_must_be_positive(self, settings_db: Settings) -> None:
        with pytest.raises(ValueError):
            cleanup(settings_db, older_than_days=0)


class TestDebris:
    def test_abandoned_workspaces_go_live_and_recent_ones_stay(self, settings_db: Settings) -> None:
        live = make_run(settings_db, status=RunStatus.RUNNING, age_days=0)
        abandoned = settings_db.workspace_root / "run_crashed"
        recent = settings_db.workspace_root / "run_just_started"
        in_use = settings_db.workspace_root / live
        for directory in (abandoned, recent, in_use):
            (directory / "attempt-1").mkdir(parents=True)
        age(abandoned, 2)
        age(in_use, 2)

        report = sweep_debris(settings_db)
        assert report.workspaces == 1
        assert not abandoned.exists()
        assert recent.exists()
        assert in_use.exists()

    def test_stale_staging_directories_go(self, settings_db: Settings) -> None:
        stale = settings_db.repo_cache_root / ".staging-dead"
        stale.mkdir(parents=True)
        age(stale, 1)
        assert sweep_debris(settings_db).staging == 1
        assert not stale.exists()

    def test_a_starting_worker_sweeps_debris(self, settings_db: Settings) -> None:
        abandoned = settings_db.workspace_root / "run_crashed"
        abandoned.mkdir(parents=True)
        age(abandoned, 2)
        worker = Worker(settings_db, concurrency=1)
        worker.start()
        worker.stop()
        assert not abandoned.exists()

    def test_scheduled_retention_runs_only_when_configured(self, settings_db: Settings) -> None:
        make_run(settings_db, status=RunStatus.FIXED, age_days=40)
        Worker(settings_db, concurrency=1).apply_retention()
        assert len(run_ids(settings_db)) == 1

        configured = settings_db.model_copy(update={"retention_days": 30.0})
        Worker(configured, concurrency=1).apply_retention()
        assert run_ids(settings_db) == set()


class TestCli:
    def test_cleanup_command(self, settings_db: Settings) -> None:
        make_run(settings_db, status=RunStatus.FIXED, age_days=40)
        runner = CliRunner()

        dry = runner.invoke(cli, ["cleanup", "--older-than", "30", "--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert "would delete: 1 run(s)" in dry.output
        assert len(run_ids(settings_db)) == 1

        real = runner.invoke(cli, ["cleanup", "--older-than", "30"])
        assert real.exit_code == 0, real.output
        assert "deleted: 1 run(s)" in real.output
        assert run_ids(settings_db) == set()

    def test_cleanup_refuses_a_sub_day_cutoff(self, settings_db: Settings) -> None:
        result = CliRunner().invoke(cli, ["cleanup", "--older-than", "0.5"])
        assert result.exit_code != 0
