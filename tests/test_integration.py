"""End-to-end tests across every layer.

These are the tests that would catch a regression nobody else would: the API
queues a run, the worker executes it, the agent indexes and retrieves and
patches, the sandbox runs the suite, and the result is readable back out of the
database and over HTTP exactly as the dashboard would read it.

Everything runs offline against the fixture repositories with the deterministic
mock adapters, so these are safe in CI.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FIXTURE_REPOS
from fastapi.testclient import TestClient
from patchpilot_api import store
from patchpilot_api.db import reset_engine, session_scope
from patchpilot_api.main import create_app
from patchpilot_api.worker import Worker
from patchpilot_core.config import get_settings
from patchpilot_core.enums import JobStatus, JobType, RunStatus

pytestmark = pytest.mark.integration


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    reset_engine()
    settings = get_settings()
    settings.ensure_dirs()
    app = create_app(settings, start_worker=False)
    with TestClient(app) as test_client:
        yield test_client
    reset_engine()


@pytest.fixture
def worker() -> Iterator[Worker]:
    instance = Worker(get_settings(), concurrency=1)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def create_run(client: TestClient, **overrides) -> str:
    payload = {
        "repository_url": str(FIXTURE_REPOS / "calc_service"),
        "issue_text": (
            "## Summary\n\n`calc_service.operations.percentage()` raises "
            "`ZeroDivisionError` when a bucket expected zero items.\n\n"
            "## Expected\n\nAn empty bucket should report 0.0%."
        ),
        "issue_title": "percentage() raises ZeroDivisionError for an empty bucket",
        "model": "mock:deterministic",
        "sandbox_backend": "local",
        "max_repair_attempts": 3,
        "timeout_seconds": 180,
    }
    payload.update(overrides)
    response = client.post("/api/v1/runs", json=payload)
    assert response.status_code == 202, response.text
    return response.json()["id"]


class TestFullRepairPipeline:
    """The headline path: a failing test is found, patched and verified."""

    @pytest.fixture(scope="class")
    def _marker(self) -> None:
        return None

    def test_a_queued_run_is_executed_and_fixed(self, client: TestClient, worker: Worker) -> None:
        run_id = create_run(client)
        assert worker.wait_for_idle(timeout=600), "the worker did not finish in time"

        detail = client.get(f"/api/v1/runs/{run_id}").json()
        run = detail["run"]

        assert run["status"] == "fixed"
        assert run["stop_reason"] == "validation-passed"
        assert run["attempts_used"] == 1
        assert run["repo_sha"].startswith("sha256:")

        # The bug really did reproduce before any patch was applied.
        assert run["baseline_reproduced"] is True
        assert detail["baseline"] is not None
        baseline = detail["baseline"]["results"][0]
        assert baseline["exit_code"] != 0
        assert "ZeroDivisionError" in (baseline["stdout"] + baseline["stderr"])

        # The final patch is a real diff against the real file.
        assert detail["final_patch"] is not None
        assert "calc_service/operations.py" in detail["final_patch"]
        assert "+    if whole == 0:" in detail["final_patch"]

    def test_the_state_machine_history_is_persisted_in_order(
        self, client: TestClient, worker: Worker
    ) -> None:
        run_id = create_run(client)
        assert worker.wait_for_idle(timeout=600)

        events = client.get(f"/api/v1/runs/{run_id}/events").json()
        visited = [event["to_state"] for event in events]
        assert visited == [
            "INGEST",
            "INDEX",
            "RETRIEVE",
            "PLAN",
            "GENERATE_PATCH",
            "VALIDATE_PATCH",
            "SANDBOX_TEST",
            "ANALYZE_RESULT",
            "REPAIR_OR_FINISH",
            "FINISHED",
        ]
        assert [event["index"] for event in events] == list(range(len(events)))
        assert all(event["reason"] for event in events)

        # Incremental polling returns only what is new.
        tail = client.get(f"/api/v1/runs/{run_id}/events?after=5").json()
        assert [event["index"] for event in tail] == [6, 7, 8, 9]

    def test_the_retrieval_trace_explains_every_selected_symbol(
        self, client: TestClient, worker: Worker
    ) -> None:
        run_id = create_run(client)
        assert worker.wait_for_idle(timeout=600)

        detail = client.get(f"/api/v1/runs/{run_id}").json()
        retrieval = detail["retrieval"]
        assert retrieval, "no retrieval trace was persisted"

        for chunk in retrieval:
            assert chunk["reasons"], f"{chunk['path']}::{chunk['symbol']} has no reason"
            assert chunk["score"] > 0
            for reason in chunk["reasons"]:
                assert reason["reason"]
                assert reason["weight"] > 0

        selected = {f"{chunk['path']}::{chunk['symbol']}" for chunk in retrieval}
        assert "calc_service/operations.py::percentage" in selected

    def test_the_attempt_carries_plan_diff_validation_and_sandbox_output(
        self, client: TestClient, worker: Worker
    ) -> None:
        run_id = create_run(client)
        assert worker.wait_for_idle(timeout=600)

        attempts = client.get(f"/api/v1/runs/{run_id}/attempts").json()
        assert len(attempts) == 1
        attempt = attempts[0]

        assert attempt["plan"]["root_cause"]
        assert attempt["plan"]["files_to_change"] == ["calc_service/operations.py"]
        assert attempt["validation"]["valid"] is True
        assert attempt["validation"]["files_changed"] == 1
        assert attempt["succeeded"] is True

        execution = attempt["execution"]
        assert execution["status"] == "completed"
        validation = [result for result in execution["results"] if result["kind"] == "validation"]
        assert validation and validation[0]["exit_code"] == 0
        assert "passed" in validation[0]["stdout"]

    def test_artifacts_survive_the_sandbox_and_are_downloadable(
        self, client: TestClient, worker: Worker
    ) -> None:
        run_id = create_run(client)
        assert worker.wait_for_idle(timeout=600)

        artifacts = client.get(f"/api/v1/runs/{run_id}/artifacts").json()
        kinds = {artifact["kind"] for artifact in artifacts}
        assert {"patch", "sandbox-log", "final-patch"} <= kinds

        for artifact in artifacts:
            response = client.get(f"/api/v1/artifacts/{artifact['id']}/download")
            assert response.status_code == 200
            assert len(response.content) == artifact["size_bytes"]

        patch = client.get(f"/api/v1/runs/{run_id}/patch.diff")
        assert patch.status_code == 200
        assert patch.headers["content-type"].startswith("text/x-diff")
        assert "if whole == 0" in patch.text

    def test_token_cost_and_latency_are_recorded(self, client: TestClient, worker: Worker) -> None:
        run_id = create_run(client)
        assert worker.wait_for_idle(timeout=600)

        detail = client.get(f"/api/v1/runs/{run_id}").json()
        run = detail["run"]
        assert run["input_tokens"] > 0
        assert run["output_tokens"] > 0
        assert run["cost_known"] is True
        assert detail["latency"]["total_ms"] > 0
        assert "SANDBOX_TEST" in detail["latency"]["per_state_ms"]

    def test_the_index_is_persisted_and_browsable(self, client: TestClient, worker: Worker) -> None:
        run_id = create_run(client)
        assert worker.wait_for_idle(timeout=600)

        detail = client.get(f"/api/v1/runs/{run_id}").json()
        repository_id = detail["repository"]["id"]

        index = client.get(f"/api/v1/repositories/{repository_id}/index").json()
        assert index["stats"]["chunks"] > 0
        assert index["embedding_provider"] == "hash"

        symbols = client.get(f"/api/v1/repositories/{repository_id}/symbols").json()
        assert any(symbol["qualified_name"] == "percentage" for symbol in symbols)

        graph = client.get(f"/api/v1/repositories/{repository_id}/graph").json()
        assert graph["resolved_edges"] >= 1
        assert any(
            edge["source_path"] == "calc_service/reporting.py"
            and edge["target_path"] == "calc_service/operations.py"
            for edge in graph["edges"]
        )

    def test_the_source_repository_is_never_modified(
        self, client: TestClient, worker: Worker
    ) -> None:
        source = FIXTURE_REPOS / "calc_service" / "calc_service" / "operations.py"
        before = source.read_text(encoding="utf-8")
        create_run(client)
        assert worker.wait_for_idle(timeout=600)
        assert source.read_text(encoding="utf-8") == before
        assert "if whole == 0" not in source.read_text(encoding="utf-8")


class TestRepairLoopEndToEnd:
    def test_a_model_that_cannot_fix_it_exhausts_its_budget_and_says_so(
        self, client: TestClient, worker: Worker
    ) -> None:
        run_id = create_run(client, model="mock:stubborn", max_repair_attempts=2)
        assert worker.wait_for_idle(timeout=600)

        detail = client.get(f"/api/v1/runs/{run_id}").json()
        assert detail["run"]["status"] == "budget-exhausted"
        assert detail["run"]["attempts_used"] == 2
        assert len(detail["attempts"]) == 2
        # Each attempt produced a patch that applied but did not fix the suite.
        for attempt in detail["attempts"]:
            assert attempt["validation"]["valid"] is True
            assert attempt["succeeded"] is False
            assert attempt["analysis"]

    def test_an_unsafe_patch_stops_the_run_and_records_the_reason(
        self, client: TestClient, worker: Worker
    ) -> None:
        run_id = create_run(client, model="mock:unsafe", max_repair_attempts=3)
        assert worker.wait_for_idle(timeout=600)

        detail = client.get(f"/api/v1/runs/{run_id}").json()
        assert detail["run"]["status"] == "patch-invalid"
        assert detail["run"]["stop_reason"] == "unsafe-patch"
        assert detail["run"]["attempts_used"] == 1

        patches = client.get(f"/api/v1/runs/{run_id}/patches").json()
        assert patches[0]["accepted"] is False
        assert "protected-path" in patches[0]["rejections"]

    def test_a_second_attempt_can_recover_from_a_bad_first_one(
        self, client: TestClient, worker: Worker
    ) -> None:
        run_id = create_run(client, model="mock:flaky")
        assert worker.wait_for_idle(timeout=600)

        detail = client.get(f"/api/v1/runs/{run_id}").json()
        assert detail["run"]["status"] == "fixed"
        assert detail["run"]["attempts_used"] == 2
        assert detail["attempts"][0]["validation"]["valid"] is False
        assert detail["attempts"][1]["succeeded"] is True


class TestOtherFixtures:
    def test_a_defect_one_import_hop_away_is_found_and_fixed(
        self, client: TestClient, worker: Worker
    ) -> None:
        """The failing tests are in analytics; the bug is in the tokenizer."""
        run_id = create_run(
            client,
            repository_url=str(FIXTURE_REPOS / "text_pipeline"),
            issue_title="Word counts split across punctuation variants",
            issue_text=(
                "`text_pipeline.analytics.top_words()` counts 'Ship.', 'Ship,' and "
                "'ship' as three different tokens, so vocabulary sizes are inflated. "
                "They should all be the same token."
            ),
        )
        assert worker.wait_for_idle(timeout=600)

        detail = client.get(f"/api/v1/runs/{run_id}").json()
        assert detail["run"]["status"] == "fixed"
        assert "text_pipeline/tokenizer.py" in detail["final_patch"]

        retrieved = {chunk["path"] for chunk in detail["retrieval"]}
        assert "text_pipeline/tokenizer.py" in retrieved

    def test_an_off_by_one_against_a_documented_contract(
        self, client: TestClient, worker: Worker
    ) -> None:
        run_id = create_run(
            client,
            repository_url=str(FIXTURE_REPOS / "task_queue"),
            issue_title="paginate() skips the first page",
            issue_text=(
                "`task_queue.pagination.paginate()` documents 1-indexed pages but "
                "page=1 returns the second page of results."
            ),
        )
        assert worker.wait_for_idle(timeout=600)

        detail = client.get(f"/api/v1/runs/{run_id}").json()
        assert detail["run"]["status"] == "fixed"
        assert "task_queue/pagination.py" in detail["final_patch"]


class TestCancellationAndResume:
    def test_a_cancelled_run_stops_and_is_recorded(self, client: TestClient) -> None:
        run_id = create_run(client)
        client.post(f"/api/v1/runs/{run_id}/cancel")

        worker = Worker(get_settings(), concurrency=1)
        worker.start()
        try:
            assert worker.wait_for_idle(timeout=300)
        finally:
            worker.stop()

        detail = client.get(f"/api/v1/runs/{run_id}").json()
        assert detail["run"]["status"] == "cancelled"

    def test_a_terminal_run_cannot_be_resumed(self, client: TestClient, worker: Worker) -> None:
        run_id = create_run(client)
        assert worker.wait_for_idle(timeout=600)
        response = client.post(f"/api/v1/runs/{run_id}/resume")
        assert response.status_code == 409

    def test_an_interrupted_job_is_requeued_and_the_history_continues(
        self, client: TestClient
    ) -> None:
        """Simulate a worker killed mid-run, then start a new worker."""
        settings = get_settings()
        run_id = create_run(client)

        # Pretend a worker claimed the job and then died: the job is stuck RUNNING
        # and the run has partial history.
        with session_scope(settings) as session:
            job = store.claim_next_job(session)
            assert job is not None
            run = store.get_run(session, run_id)
            run.status = str(RunStatus.RUNNING)
            from patchpilot_core.enums import RunState
            from patchpilot_core.models import StateTransition

            for index in range(3):
                store.record_event(
                    session,
                    run_id,
                    StateTransition(
                        index=index,
                        from_state=None,
                        to_state=RunState.INGEST,
                        reason=f"partial history {index}",
                    ),
                )

        replacement = Worker(settings, concurrency=1)
        recovered = replacement.recover_orphans()
        assert recovered == 1

        replacement.start()
        try:
            assert replacement.wait_for_idle(timeout=600)
        finally:
            replacement.stop()

        detail = client.get(f"/api/v1/runs/{run_id}").json()
        assert detail["run"]["status"] == "fixed"

        indices = [event["index"] for event in detail["events"]]
        # The pre-crash history is intact and the resumed run appended to it.
        assert indices[:3] == [0, 1, 2]
        assert indices == sorted(indices)
        assert len(set(indices)) == len(indices)
        assert max(indices) >= 12

    def test_a_job_that_keeps_dying_is_eventually_failed(self, client: TestClient) -> None:
        settings = get_settings()
        with session_scope(settings) as session:
            job = store.enqueue_job(session, JobType.AGENT_RUN, {"run_id": "run_x"})
            job.status = str(JobStatus.RUNNING)
            job.attempts = 3

        worker = Worker(settings, concurrency=1)
        assert worker.recover_orphans() == 0

        with session_scope(settings) as session:
            refreshed = store.get_job(session, job.id)
            assert refreshed.status == str(JobStatus.FAILED)
            assert "abandoned" in (refreshed.error or "")


class TestBenchmarkEndToEnd:
    @pytest.mark.slow
    def test_a_benchmark_runs_and_produces_both_reports(
        self, client: TestClient, worker: Worker
    ) -> None:
        response = client.post(
            "/api/v1/benchmarks",
            json={
                "dataset": "patchpilot-fixtures",
                "models": ["mock:deterministic", "mock:stubborn"],
                "tags": ["guard-clause"],
            },
        )
        assert response.status_code == 202
        assert worker.wait_for_idle(timeout=900)

        benchmark = client.get("/api/v1/benchmarks").json()[0]
        assert benchmark["status"] == "succeeded"

        detail = client.get(f"/api/v1/benchmarks/{benchmark['id']}").json()
        assert len(detail["results"]) == 2  # one task, two models
        by_model = {result["model"]: result for result in detail["results"]}
        assert by_model["mock:deterministic"]["passed"] is True
        assert by_model["mock:stubborn"]["passed"] is False
        assert by_model["mock:deterministic"]["run_id"]

        summaries = {item["model"]: item for item in detail["report"]["summaries"]}
        assert summaries["mock:deterministic"]["pass_rate"] == 1.0
        assert summaries["mock:stubborn"]["retries_per_task"] > 0

        json_report = client.get(f"/api/v1/benchmarks/{benchmark['id']}/report.json")
        assert json_report.status_code == 200
        assert json.loads(json_report.text)["dataset"] == "patchpilot-fixtures"

        markdown = client.get(f"/api/v1/benchmarks/{benchmark['id']}/report.md")
        assert markdown.status_code == 200
        assert "# Benchmark report" in markdown.text
        assert "Small sample" in markdown.text

    @pytest.mark.slow
    def test_filtering_results_by_tag_and_outcome(self, client: TestClient, worker: Worker) -> None:
        client.post(
            "/api/v1/benchmarks",
            json={
                "dataset": "patchpilot-fixtures",
                "models": ["mock:deterministic", "mock:stubborn"],
                "tags": ["guard-clause"],
            },
        )
        assert worker.wait_for_idle(timeout=900)
        benchmark_id = client.get("/api/v1/benchmarks").json()[0]["id"]

        passing = client.get(f"/api/v1/benchmarks/{benchmark_id}?result=pass").json()
        assert all(result["passed"] for result in passing["results"])

        tagged = client.get(f"/api/v1/benchmarks/{benchmark_id}?tag=python").json()
        assert tagged["results"]
        missing = client.get(f"/api/v1/benchmarks/{benchmark_id}?tag=nosuchtag").json()
        assert missing["results"] == []
