"""Job leases, and runs that must never be left looking alive.

The worker used to treat every RUNNING job as orphaned when it started. With
one process that was true; with two -- a second API replica, or a separate
``patchpilot worker`` -- it requeued jobs a live worker was executing, and the
same run then executed twice.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import FIXTURE_REPOS
from fastapi.testclient import TestClient
from patchpilot_api import services, store
from patchpilot_api.db import reset_engine, session_scope
from patchpilot_api.main import create_app
from patchpilot_api.worker import Worker
from patchpilot_core.config import get_settings
from patchpilot_core.enums import JobStatus, JobType, RunStatus


@pytest.fixture
def client() -> Iterator[TestClient]:
    reset_engine()
    app = create_app(get_settings(), start_worker=False)
    with TestClient(app) as test_client:
        yield test_client
    reset_engine()


def create_run(client: TestClient) -> str:
    response = client.post(
        "/api/v1/runs",
        json={
            "repository_url": str(FIXTURE_REPOS / "calc_service"),
            "issue_text": "percentage(0, 0) raises ZeroDivisionError.",
            "sandbox_backend": "local",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def claim(worker_id: str, *, age_seconds: float = 0.0) -> str:
    """Claim the next job as ``worker_id``, with a heartbeat ``age_seconds`` old."""
    with session_scope() as session:
        job = store.claim_next_job(session, worker_id=worker_id)
        assert job is not None
        if age_seconds:
            job.heartbeat_at = store.utcnow() - timedelta(seconds=age_seconds)
        return job.id


def job_row(job_id: str):
    with session_scope() as session:
        return store.get_job(session, job_id)


class TestLeases:
    def test_a_live_job_is_not_stolen_by_another_worker(self, client: TestClient) -> None:
        create_run(client)
        job_id = claim("worker-a")

        newcomer = Worker(get_settings(), concurrency=1)
        assert newcomer.recover_orphans() == 0
        assert job_row(job_id).status == str(JobStatus.RUNNING)

    def test_a_stale_job_is_requeued_and_claimable(self, client: TestClient) -> None:
        create_run(client)
        lease = get_settings().worker_lease_seconds
        job_id = claim("worker-a", age_seconds=lease + 5)

        assert Worker(get_settings(), concurrency=1).recover_orphans() == 1
        row = job_row(job_id)
        assert row.status == str(JobStatus.QUEUED)
        assert row.worker_id is None
        assert claim("worker-b") == job_id

    def test_a_heartbeat_keeps_the_lease(self, client: TestClient) -> None:
        create_run(client)
        lease = get_settings().worker_lease_seconds
        job_id = claim("worker-a", age_seconds=lease - 1)

        with session_scope() as session:
            assert store.heartbeat_jobs(session, [job_id], "worker-a") == 1
            # Another worker's id renews nothing.
            assert store.heartbeat_jobs(session, [job_id], "worker-z") == 0
        with session_scope() as session:
            recovery = store.requeue_orphaned_jobs(session, lease_seconds=lease)
        assert recovery.requeued == []

    def test_the_worker_heartbeats_the_jobs_it_runs(self, client: TestClient) -> None:
        create_run(client)
        worker = Worker(get_settings(), concurrency=1)
        job_id = claim(worker.worker_id, age_seconds=30)
        worker._active.add(job_id)
        before = job_row(job_id).heartbeat_at

        assert worker.heartbeat() == 1
        assert job_row(job_id).heartbeat_at > before

    def test_a_worker_that_lost_its_lease_does_not_overwrite_the_new_owner(
        self, client: TestClient
    ) -> None:
        create_run(client)
        job_id = claim("worker-a", age_seconds=10_000)
        with session_scope() as session:
            store.requeue_orphaned_jobs(session, lease_seconds=60)
        claim("worker-b")

        with session_scope() as session:
            written = store.finish_job(
                session, job_id, status=JobStatus.FAILED, error="late", worker_id="worker-a"
            )
        assert written is False
        row = job_row(job_id)
        assert row.status == str(JobStatus.RUNNING)
        assert row.worker_id == "worker-b"


class TestRunsAreNeverLeftRunning:
    def test_an_abandoned_job_closes_its_run(self, client: TestClient) -> None:
        run_id = create_run(client)
        with session_scope() as session:
            job = store.claim_next_job(session, worker_id="worker-a")
            assert job is not None
            job.attempts = 3
            job.heartbeat_at = None
            store.get_run(session, run_id).status = str(RunStatus.RUNNING)

        Worker(get_settings(), concurrency=1).recover_orphans()

        run = client.get(f"/api/v1/runs/{run_id}").json()["run"]
        assert run["status"] == "error"
        assert "abandoned" in run["error"]
        assert run["finished_at"]

    def test_a_crashing_job_closes_its_run(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = create_run(client)

        def crash(payload, settings):  # type: ignore[no-untyped-def]
            with session_scope(settings) as session:
                store.get_run(session, payload["run_id"]).status = str(RunStatus.RUNNING)
            raise RuntimeError("the database went away while finalising")

        monkeypatch.setitem(services.JOB_HANDLERS, str(JobType.AGENT_RUN), crash)
        worker = Worker(get_settings(), concurrency=1)
        worker.start()
        try:
            assert worker.wait_for_idle(timeout=30)
        finally:
            worker.stop()

        run = client.get(f"/api/v1/runs/{run_id}").json()["run"]
        assert run["status"] == "error"
        assert "database went away" in run["error"]

    def test_a_failed_benchmark_job_fails_the_benchmark(self, client: TestClient) -> None:
        with session_scope() as session:
            benchmark = store.create_benchmark(
                session, dataset="x", models=["mock:deterministic"], tags=[]
            )
            identifier = benchmark.id
        services.handle_job_failure(
            str(JobType.BENCHMARK_RUN), {"benchmark_id": identifier}, "boom", get_settings()
        )
        body = client.get(f"/api/v1/benchmarks/{identifier}").json()
        assert body["status"] == "failed"
        assert body["error"] == "boom"


class TestResumeAndCancel:
    def test_resume_is_refused_while_the_run_has_a_live_job(self, client: TestClient) -> None:
        """This used to enqueue a second job and execute the run twice at once."""
        run_id = create_run(client)
        response = client.post(f"/api/v1/runs/{run_id}/resume")
        assert response.status_code == 409
        assert response.json()["context"]["job_id"]

        claim("worker-a")
        assert client.post(f"/api/v1/runs/{run_id}/resume").status_code == 409

        with session_scope() as session:
            assert len(store.active_jobs_for(session, JobType.AGENT_RUN, "run_id", run_id)) == 1

    def test_resume_is_allowed_once_the_job_has_ended(self, client: TestClient) -> None:
        run_id = create_run(client)
        job_id = claim("worker-a")
        with session_scope() as session:
            store.finish_job(session, job_id, status=JobStatus.FAILED, error="killed")
            store.get_run(session, run_id).status = str(RunStatus.RUNNING)

        response = client.post(f"/api/v1/runs/{run_id}/resume")
        assert response.status_code == 202
        assert response.json()["status"] == "queued"

    def test_cancelling_a_queued_run_is_immediate(self, client: TestClient) -> None:
        run_id = create_run(client)
        response = client.post(f"/api/v1/runs/{run_id}/cancel")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "cancelled"
        assert body["finished_at"]
        with session_scope() as session:
            assert store.count_jobs(session, JobStatus.QUEUED) == 0

    def test_cancelling_a_running_run_sets_the_flag_only(self, client: TestClient) -> None:
        run_id = create_run(client)
        claim("worker-a")
        with session_scope() as session:
            store.get_run(session, run_id).status = str(RunStatus.RUNNING)

        body = client.post(f"/api/v1/runs/{run_id}/cancel").json()
        assert body["status"] == "running"
        with session_scope() as session:
            assert store.cancel_requested(session, run_id)


class TestHousekeeping:
    def test_a_run_leaves_no_workspace_directory_behind(self, fixture_repo: Path) -> None:
        from patchpilot_agent import run_agent
        from patchpilot_core.models import IssueSpec, RepositorySpec, RunConfig

        settings = get_settings()
        outcome = run_agent(
            repository=RepositorySpec(url=str(fixture_repo)),
            issue=IssueSpec(title="percentage(0, 0) raises ZeroDivisionError"),
            config=RunConfig(
                model="mock:deterministic",
                validation_command="python -m pytest -q",
                sandbox_backend="local",
                limits=settings.sandbox_limits(),
            ),
            settings=settings,
        )
        assert outcome.summary.run_id
        assert not (settings.workspace_root / outcome.summary.run_id).exists()
