"""HTTP API, persistence, migrations and the background worker."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FIXTURE_REPOS
from fastapi.testclient import TestClient
from patchpilot_api import store
from patchpilot_api.db import reset_engine, session_scope
from patchpilot_api.main import create_app
from patchpilot_api.migrations import current_revision, head_revision, upgrade
from patchpilot_api.worker import Worker
from patchpilot_core.config import get_settings
from patchpilot_core.enums import JobStatus, JobType, RunState
from patchpilot_core.models import IssueSpec, RepositorySpec, RunConfig, StateTransition


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    reset_engine()
    settings = get_settings()
    settings.ensure_dirs()
    app = create_app(settings, start_worker=False)
    with TestClient(app) as test_client:
        yield test_client
    reset_engine()


def run_payload(**overrides) -> dict:
    payload = {
        "repository_url": str(FIXTURE_REPOS / "calc_service"),
        "issue_text": (
            "percentage(0, 0) raises ZeroDivisionError. An empty bucket should report 0%."
        ),
        "model": "mock:deterministic",
        "sandbox_backend": "local",
        "max_repair_attempts": 2,
        "timeout_seconds": 120,
    }
    payload.update(overrides)
    return payload


class TestMigrations:
    def test_the_schema_is_created_and_matches_head(self, client: TestClient) -> None:
        settings = get_settings()
        assert current_revision(settings) == head_revision(settings)

    def test_upgrading_twice_is_a_no_op(self, client: TestClient) -> None:
        settings = get_settings()
        before = current_revision(settings)
        upgrade(settings)
        assert current_revision(settings) == before

    def test_migrating_in_process_leaves_application_logging_alone(self) -> None:
        """alembic.ini's fileConfig would disable every existing logger."""
        reset_engine()
        settings = get_settings()
        settings.ensure_dirs()
        existing = logging.getLogger("patchpilot_api.services")
        handlers = list(logging.getLogger().handlers)
        try:
            assert current_revision(settings) is None  # a fresh database
            upgrade(settings)
            assert current_revision(settings) == head_revision(settings)
            assert not existing.disabled
            assert logging.getLogger().handlers == handlers
        finally:
            reset_engine()


class TestSystemEndpoints:
    def test_health(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_system_reports_the_real_configuration(self, client: TestClient) -> None:
        body = client.get("/api/v1/system").json()
        assert body["embedding_provider"] == "hash"
        assert body["vector_store"] == "local"
        assert body["sandbox"]["backend"] in {"docker", "local"}
        assert isinstance(body["sandbox"]["isolated"], bool)

    def test_sandbox_endpoint_lists_the_controls(self, client: TestClient) -> None:
        body = client.get("/api/v1/system/sandbox").json()
        assert "controls" in body
        assert body["reason"]

    def test_openapi_document_is_complete(self, client: TestClient) -> None:
        document = client.get("/openapi.json").json()
        paths = document["paths"]
        for expected in (
            "/api/v1/runs",
            "/api/v1/runs/{run_id}",
            "/api/v1/runs/{run_id}/events",
            "/api/v1/repositories",
            "/api/v1/models",
            "/api/v1/datasets",
            "/api/v1/benchmarks",
            "/api/v1/system",
        ):
            assert expected in paths, f"{expected} missing from the OpenAPI document"
        assert document["info"]["title"] == "PatchPilot API"

    def test_correlation_id_is_echoed(self, client: TestClient) -> None:
        response = client.get("/api/v1/system", headers={"x-correlation-id": "abc123"})
        assert response.headers["x-correlation-id"] == "abc123"


class TestModelsEndpoint:
    def test_lists_configured_and_unconfigured_models(self, client: TestClient) -> None:
        models = {item["id"]: item for item in client.get("/api/v1/models").json()}
        assert models["mock:deterministic"]["available"] is True
        assert models["openai:gpt-4o"]["available"] is False
        assert "PATCHPILOT_OPENAI_API_KEY" in models["openai:gpt-4o"]["reason"]


class TestRepositoryEndpoints:
    def test_register_and_list(self, client: TestClient) -> None:
        created = client.post(
            "/api/v1/repositories", json={"url": str(FIXTURE_REPOS / "calc_service")}
        )
        assert created.status_code == 201
        identifier = created.json()["id"]
        assert any(item["id"] == identifier for item in client.get("/api/v1/repositories").json())

    def test_indexing_is_queued_not_executed_inline(self, client: TestClient) -> None:
        identifier = client.post(
            "/api/v1/repositories", json={"url": str(FIXTURE_REPOS / "calc_service")}
        ).json()["id"]
        response = client.post(f"/api/v1/repositories/{identifier}/index")
        assert response.status_code == 202
        assert response.json()["status"] == "queued"

    def test_index_endpoints_explain_themselves_before_indexing(self, client: TestClient) -> None:
        identifier = client.post(
            "/api/v1/repositories", json={"url": str(FIXTURE_REPOS / "calc_service")}
        ).json()["id"]
        response = client.get(f"/api/v1/repositories/{identifier}/index")
        assert response.status_code == 404
        assert "index" in response.json()["remediation"].lower()

    def test_unknown_repository_is_a_clean_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/repositories/repo_nope")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"


class TestRunEndpoints:
    def test_creating_a_run_returns_202_and_queues_a_job(self, client: TestClient) -> None:
        response = client.post("/api/v1/runs", json=run_payload())
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["model"] == "mock:deterministic"

        jobs = client.get("/api/v1/jobs").json()
        assert any(job["type"] == str(JobType.AGENT_RUN) for job in jobs)

    def test_a_run_needs_an_issue(self, client: TestClient) -> None:
        response = client.post("/api/v1/runs", json=run_payload(issue_text=None, issue_number=None))
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    def test_rejects_an_out_of_range_retry_budget(self, client: TestClient) -> None:
        response = client.post("/api/v1/runs", json=run_payload(max_repair_attempts=99))
        assert response.status_code == 422

    def test_detail_is_available_immediately_after_creation(self, client: TestClient) -> None:
        identifier = client.post("/api/v1/runs", json=run_payload()).json()["id"]
        detail = client.get(f"/api/v1/runs/{identifier}").json()
        assert detail["run"]["id"] == identifier
        assert detail["events"] == []
        assert detail["retrieval"] == []
        assert detail["issue"]["body"]

    def test_listing_filters_by_status_and_model(self, client: TestClient) -> None:
        client.post("/api/v1/runs", json=run_payload())
        assert client.get("/api/v1/runs?status=queued").json()["total"] == 1
        assert client.get("/api/v1/runs?status=fixed").json()["total"] == 0
        assert client.get("/api/v1/runs?model=mock:deterministic").json()["total"] == 1

    def test_cancelling_a_queued_run_sets_the_flag(self, client: TestClient) -> None:
        identifier = client.post("/api/v1/runs", json=run_payload()).json()["id"]
        assert client.post(f"/api/v1/runs/{identifier}/cancel").status_code == 200
        with session_scope(get_settings()) as session:
            assert store.cancel_requested(session, identifier)

    def test_unknown_run_is_a_clean_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/runs/run_nope")
        assert response.status_code == 404
        assert response.json()["message"]

    def test_downloading_a_patch_that_does_not_exist_explains_why(self, client: TestClient) -> None:
        identifier = client.post("/api/v1/runs", json=run_payload()).json()["id"]
        response = client.get(f"/api/v1/runs/{identifier}/patch.diff")
        assert response.status_code == 404


class TestDatasetAndBenchmarkEndpoints:
    def test_lists_the_bundled_dataset(self, client: TestClient) -> None:
        datasets = client.get("/api/v1/datasets").json()
        assert any(item["name"] == "patchpilot-fixtures" for item in datasets)
        detail = client.get("/api/v1/datasets/patchpilot-fixtures").json()
        assert detail["task_count"] == 3
        assert "off-by-one" in detail["tags"]

    def test_queues_a_benchmark(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/benchmarks",
            json={"dataset": "patchpilot-fixtures", "models": ["mock:deterministic"], "tags": []},
        )
        assert response.status_code == 202
        assert response.json()["type"] == str(JobType.BENCHMARK_RUN)
        assert client.get("/api/v1/benchmarks").json()

    def test_a_report_is_404_until_the_benchmark_finishes(self, client: TestClient) -> None:
        client.post(
            "/api/v1/benchmarks",
            json={"dataset": "patchpilot-fixtures", "models": ["mock:deterministic"], "tags": []},
        )
        identifier = client.get("/api/v1/benchmarks").json()[0]["id"]
        assert client.get(f"/api/v1/benchmarks/{identifier}/report.md").status_code == 404


class TestJobQueue:
    def test_a_job_is_claimed_exactly_once(self, client: TestClient) -> None:
        settings = get_settings()
        with session_scope(settings) as session:
            store.enqueue_job(session, JobType.AGENT_RUN, {"run_id": "run_x"})

        claimed = []
        for _ in range(3):
            with session_scope(settings) as session:
                job = store.claim_next_job(session)
                if job is not None:
                    claimed.append(job.id)
        assert len(claimed) == 1

    def test_an_unknown_job_type_fails_the_job_not_the_worker(self, client: TestClient) -> None:
        settings = get_settings()
        with session_scope(settings) as session:
            store.enqueue_job(session, "not_a_real_type", {})  # type: ignore[arg-type]

        worker = Worker(settings, concurrency=1)
        worker.start()
        try:
            assert worker.wait_for_idle(timeout=30)
        finally:
            worker.stop()

        with session_scope(settings) as session:
            job = store.list_jobs(session)[0]
            assert job.status == str(JobStatus.FAILED)
            assert "unknown job type" in (job.error or "")

    def test_a_failing_handler_is_recorded_and_the_worker_survives(
        self, client: TestClient
    ) -> None:
        settings = get_settings()
        with session_scope(settings) as session:
            store.enqueue_job(session, JobType.AGENT_RUN, {"run_id": "run_does_not_exist"})

        worker = Worker(settings, concurrency=1)
        worker.start()
        try:
            assert worker.wait_for_idle(timeout=60)
        finally:
            worker.stop()

        with session_scope(settings) as session:
            job = store.list_jobs(session)[0]
            assert job.status == str(JobStatus.FAILED)
            assert "not found" in (job.error or "")


class TestStore:
    def test_artifacts_are_written_and_hashed(self, client: TestClient) -> None:
        settings = get_settings()
        with session_scope(settings) as session:
            repository = store.upsert_repository(session, RepositorySpec(url="local/path"))
            issue = store.create_issue(session, repository, IssueSpec(title="t", body="b"))
            run = store.create_run(session, repository=repository, issue=issue, config=RunConfig())
            artifact = store.write_artifact(
                session,
                run.id,
                attempt=1,
                kind="patch",
                filename="attempt-1.diff",
                content="--- a/x\n+++ b/x\n",
                settings=settings,
            )
        assert Path(artifact.path).is_file()
        assert artifact.sha256
        assert artifact.size_bytes > 0

    def test_an_artifact_filename_cannot_escape_the_run_directory(self, client: TestClient) -> None:
        settings = get_settings()
        with session_scope(settings) as session:
            repository = store.upsert_repository(session, RepositorySpec(url="local/path"))
            issue = store.create_issue(session, repository, IssueSpec(title="t", body="b"))
            run = store.create_run(session, repository=repository, issue=issue, config=RunConfig())
            artifact = store.write_artifact(
                session,
                run.id,
                attempt=1,
                kind="patch",
                filename="../../escaped.diff",
                content="x",
                settings=settings,
            )
        assert Path(artifact.path).parent == settings.artifact_root / run.id

    def test_repositories_are_deduplicated_by_url_and_branch(self, client: TestClient) -> None:
        settings = get_settings()
        with session_scope(settings) as session:
            first = store.upsert_repository(session, RepositorySpec(url="repo", branch="main"))
            second = store.upsert_repository(session, RepositorySpec(url="repo", branch="main"))
            other = store.upsert_repository(session, RepositorySpec(url="repo", branch="dev"))
        assert first.id == second.id
        assert other.id != first.id

    def test_events_are_append_only_and_ordered(self, client: TestClient) -> None:
        settings = get_settings()
        with session_scope(settings) as session:
            repository = store.upsert_repository(session, RepositorySpec(url="repo"))
            issue = store.create_issue(session, repository, IssueSpec(title="t", body="b"))
            run = store.create_run(session, repository=repository, issue=issue, config=RunConfig())
            for index in range(3):
                store.record_event(
                    session,
                    run.id,
                    StateTransition(
                        index=index,
                        from_state=None,
                        to_state=RunState.INGEST,
                        reason=f"step {index}",
                    ),
                )
        with session_scope(settings) as session:
            events = store.list_events(session, run.id)
            assert [event.index for event in events] == [0, 1, 2]
            assert [event.index for event in store.list_events(session, run.id, after=0)] == [1, 2]
