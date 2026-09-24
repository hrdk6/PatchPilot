"""The LangGraph repair loop.

These are the tests that matter most for safety: every terminal state must be
reachable, the retry budget must be impossible to exceed, and an unsafe patch
must stop the run rather than cost another attempt.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from patchpilot_agent import RunObserver, run_agent
from patchpilot_agent import graph as graph_module
from patchpilot_core.enums import RunState, RunStatus, StopReason
from patchpilot_core.models import (
    IssueSpec,
    RepositorySpec,
    RunConfig,
    SandboxLimits,
    TokenUsage,
)


def config_for(variant: str, attempts: int = 3) -> RunConfig:
    return RunConfig(
        model=f"mock:{variant}",
        max_repair_attempts=attempts,
        sandbox_backend="local",
        limits=SandboxLimits(timeout_seconds=120, memory_mb=512),
    )


def run_variant(
    variant: str,
    repo: Path,
    issue: IssueSpec,
    settings,
    *,
    attempts: int = 3,
    observer: RunObserver | None = None,
    cache: dict | None = None,
):
    return run_agent(
        repository=RepositorySpec(url=str(repo), name=repo.name),
        issue=issue,
        config=config_for(variant, attempts),
        settings=settings,
        observer=observer,
        index_cache=cache if cache is not None else {},
    )


@pytest.fixture(scope="module")
def _shared_cache() -> dict:
    return {}


class TestHappyPath:
    def test_a_fixable_bug_ends_in_fixed(self, settings, fixture_repo, sample_issue) -> None:
        outcome = run_variant("deterministic", fixture_repo, sample_issue, settings)
        assert outcome.summary.status is RunStatus.FIXED
        assert outcome.summary.stop_reason is StopReason.VALIDATION_PASSED
        assert outcome.summary.attempts_used == 1
        assert outcome.summary.final_patch is not None
        assert "if whole == 0" in outcome.summary.final_patch.diff

    def test_it_visits_every_state_in_order(self, settings, fixture_repo, sample_issue) -> None:
        outcome = run_variant("deterministic", fixture_repo, sample_issue, settings)
        visited = [transition.to_state for transition in outcome.state["transitions"]]
        expected = [
            RunState.INGEST,
            RunState.INDEX,
            RunState.RETRIEVE,
            RunState.PLAN,
            RunState.GENERATE_PATCH,
            RunState.VALIDATE_PATCH,
            RunState.SANDBOX_TEST,
            RunState.ANALYZE_RESULT,
            RunState.REPAIR_OR_FINISH,
            RunState.FINISHED,
        ]
        assert visited == expected

    def test_every_transition_records_a_reason_and_a_duration(
        self, settings, fixture_repo, sample_issue
    ) -> None:
        outcome = run_variant("deterministic", fixture_repo, sample_issue, settings)
        transitions = outcome.state["transitions"]
        assert [t.index for t in transitions] == list(range(len(transitions)))
        assert all(t.reason for t in transitions)
        assert all(t.duration_ms >= 0 for t in transitions)
        assert transitions[0].from_state is None

    def test_the_baseline_runs_before_any_patch(self, settings, fixture_repo, sample_issue) -> None:
        outcome = run_variant("deterministic", fixture_repo, sample_issue, settings)
        assert outcome.state["baseline_reproduced"] is True
        assert outcome.state["baseline"] is not None
        assert not outcome.state["baseline"].validation_passed

    def test_usage_cost_and_latency_are_accounted_for(
        self, settings, fixture_repo, sample_issue
    ) -> None:
        summary = run_variant("deterministic", fixture_repo, sample_issue, settings).summary
        assert summary.usage.total_tokens > 0
        assert summary.cost is not None and summary.cost.pricing_known
        assert summary.latency.total_ms > 0
        assert set(summary.latency.per_state_ms) >= {"INGEST", "RETRIEVE", "SANDBOX_TEST"}

    def test_the_run_is_pinned_to_an_immutable_identifier(
        self, settings, fixture_repo, sample_issue
    ) -> None:
        summary = run_variant("deterministic", fixture_repo, sample_issue, settings).summary
        assert summary.repo_sha
        assert summary.repo_sha.startswith("sha256:")


class TestRepairLoop:
    def test_a_bad_first_attempt_is_repaired_on_the_second(
        self, settings, fixture_repo, sample_issue
    ) -> None:
        outcome = run_variant("flaky", fixture_repo, sample_issue, settings)
        assert outcome.summary.status is RunStatus.FIXED
        assert outcome.summary.attempts_used == 2
        attempts = outcome.state["attempts"]
        assert attempts[0].validation is not None and not attempts[0].validation.valid
        assert attempts[1].succeeded

    def test_the_failure_is_fed_back_into_the_next_attempt(
        self, settings, fixture_repo, sample_issue
    ) -> None:
        outcome = run_variant("stubborn", fixture_repo, sample_issue, settings)
        assert outcome.state["failure_excerpt"]
        assert outcome.state["attempts"][0].analysis

    def test_the_retry_budget_is_never_exceeded(self, settings, fixture_repo, sample_issue) -> None:
        for budget in (1, 2, 3):
            outcome = run_variant("stubborn", fixture_repo, sample_issue, settings, attempts=budget)
            assert outcome.summary.attempts_used == budget
            assert len(outcome.state["attempts"]) == budget

    def test_exhausting_the_budget_is_its_own_terminal_state(
        self, settings, fixture_repo, sample_issue
    ) -> None:
        outcome = run_variant("stubborn", fixture_repo, sample_issue, settings, attempts=2)
        assert outcome.summary.status is RunStatus.BUDGET_EXHAUSTED
        assert outcome.summary.stop_reason is StopReason.BUDGET_EXHAUSTED

    def test_each_attempt_starts_from_the_pristine_checkout(
        self, settings, fixture_repo, sample_issue
    ) -> None:
        """The stubborn model inserts a marker comment; it must never accumulate."""
        outcome = run_variant("stubborn", fixture_repo, sample_issue, settings, attempts=3)
        for record in outcome.state["attempts"]:
            assert record.patch is not None
            assert record.patch.diff.count("reviewed during repair attempt") == 1
        # The source checkout is untouched by any attempt.
        assert "reviewed during repair attempt" not in (
            fixture_repo / "calc_service" / "operations.py"
        ).read_text(encoding="utf-8")


class TestTerminalStates:
    def test_an_unsafe_patch_stops_immediately(self, settings, fixture_repo, sample_issue) -> None:
        outcome = run_variant("unsafe", fixture_repo, sample_issue, settings, attempts=3)
        assert outcome.summary.status is RunStatus.PATCH_INVALID
        assert outcome.summary.stop_reason is StopReason.UNSAFE_PATCH
        # Budget was 3, but a policy violation must not buy another attempt.
        assert outcome.summary.attempts_used == 1

    def test_a_model_that_never_produces_a_diff_ends_patch_invalid(
        self, settings, fixture_repo, sample_issue
    ) -> None:
        outcome = run_variant("broken", fixture_repo, sample_issue, settings, attempts=2)
        assert outcome.summary.status is RunStatus.PATCH_INVALID
        assert outcome.state["attempts"][0].plan is None
        assert outcome.state["attempts"][0].plan_error

    def test_an_unavailable_sandbox_is_reported_as_such(
        self, settings, fixture_repo, sample_issue, monkeypatch
    ) -> None:
        monkeypatch.setenv("PATCHPILOT_ALLOW_LOCAL_SANDBOX", "false")
        settings.allow_local_sandbox = False
        outcome = run_variant("deterministic", fixture_repo, sample_issue, settings)
        assert outcome.summary.status is RunStatus.SANDBOX_FAILED
        assert outcome.summary.stop_reason is StopReason.SANDBOX_UNAVAILABLE
        assert outcome.summary.attempts_used == 0

    def test_a_missing_repository_fails_before_any_execution(
        self, settings, sample_issue, tmp_path
    ) -> None:
        outcome = run_agent(
            repository=RepositorySpec(url=str(tmp_path / "nope")),
            issue=sample_issue,
            config=config_for("deterministic"),
            settings=settings,
        )
        assert outcome.summary.status is RunStatus.ERROR
        assert "does not exist" in (outcome.summary.error or "")

    def test_a_repository_with_no_tests_is_refused_with_a_reason(
        self, settings, sample_issue, tmp_path
    ) -> None:
        empty = tmp_path / "no_tests"
        empty.mkdir()
        (empty / "main.py").write_text("print('hi')\n", encoding="utf-8")
        outcome = run_agent(
            repository=RepositorySpec(url=str(empty)),
            issue=sample_issue,
            config=config_for("deterministic"),
            settings=settings,
        )
        assert outcome.summary.status is RunStatus.ERROR
        assert "test command" in (outcome.summary.error or "")

    def test_cancellation_is_honoured_between_nodes(
        self, settings, fixture_repo, sample_issue
    ) -> None:
        observer = RunObserver(should_cancel=lambda: True)
        outcome = run_variant(
            "deterministic", fixture_repo, sample_issue, settings, observer=observer
        )
        assert outcome.summary.status is RunStatus.CANCELLED
        assert outcome.summary.stop_reason is StopReason.CANCELLED

    def test_a_failing_observer_never_breaks_a_run(
        self, settings, fixture_repo, sample_issue
    ) -> None:
        def explode(_event) -> None:
            raise RuntimeError("the database is on fire")

        observer = RunObserver(on_transition=explode, on_attempt=explode, on_state=explode)
        outcome = run_variant(
            "deterministic", fixture_repo, sample_issue, settings, observer=observer
        )
        assert outcome.summary.status is RunStatus.FIXED

    def test_a_crash_mid_run_keeps_the_history_and_closes_the_timeline(
        self, settings, fixture_repo, sample_issue, monkeypatch
    ) -> None:
        def explode(*_args, **_kwargs):
            raise RuntimeError("the retriever is on fire")

        monkeypatch.setattr(graph_module.HybridRetriever, "retrieve", explode)
        outcome = run_variant("deterministic", fixture_repo, sample_issue, settings)

        assert outcome.summary.status is RunStatus.ERROR
        assert "the retriever is on fire" in (outcome.summary.error or "")
        visited = [transition.to_state for transition in outcome.state["transitions"]]
        # Work recorded before the crash survives, and the timeline says how it ended.
        assert visited[:2] == [RunState.INGEST, RunState.INDEX]
        assert visited[-1] is RunState.FINISHED
        assert outcome.summary.repo_sha
        assert outcome.summary.state is RunState.FINISHED


class TestAttemptAccounting:
    def test_each_attempt_reports_only_its_own_tokens(
        self, settings, fixture_repo, sample_issue
    ) -> None:
        outcome = run_variant("stubborn", fixture_repo, sample_issue, settings, attempts=3)
        attempts = outcome.state["attempts"]
        assert len(attempts) == 3
        assert all(record.usage.total_tokens > 0 for record in attempts)
        # Per-attempt usage partitions the run total; cumulative figures would not.
        assert sum((record.usage for record in attempts), TokenUsage()) == outcome.summary.usage

    def test_each_attempt_is_timed_from_retrieve_to_analysis(
        self, settings, fixture_repo, sample_issue
    ) -> None:
        outcome = run_variant("deterministic", fixture_repo, sample_issue, settings)
        record = outcome.state["attempts"][0]
        sandbox_ms = next(
            transition.duration_ms
            for transition in outcome.state["transitions"]
            if transition.to_state is RunState.SANDBOX_TEST
        )
        assert record.duration_ms >= sandbox_ms > 0


class TestObservability:
    def test_transitions_and_attempts_are_streamed_as_they_happen(
        self, settings, fixture_repo, sample_issue
    ) -> None:
        transitions: list = []
        attempts: list = []
        observer = RunObserver(on_transition=transitions.append, on_attempt=attempts.append)
        outcome = run_variant("flaky", fixture_repo, sample_issue, settings, observer=observer)
        assert len(transitions) == len(outcome.state["transitions"])
        assert len(attempts) == outcome.summary.attempts_used == 2
        assert [record.attempt for record in attempts] == [1, 2]

    def test_the_retrieval_trace_is_attached_to_the_state(
        self, settings, pipeline_repo, settings_issue=None
    ) -> None:
        issue = IssueSpec(
            title="counts split across punctuation",
            body="top_words counts 'Ship.' and 'ship' separately in the analytics output",
        )
        outcome = run_variant("deterministic", pipeline_repo, issue, settings)
        package = outcome.state["context"]
        assert package is not None and package.chunks
        assert all(item.components for item in package.chunks)
        assert "text_pipeline/tokenizer.py" in package.paths()

    def test_the_index_is_reused_across_runs_of_the_same_sha(
        self, settings, fixture_repo, sample_issue
    ) -> None:
        cache: dict = {}
        first = run_variant("deterministic", fixture_repo, sample_issue, settings, cache=cache)
        assert len(cache) == 1
        second = run_variant("deterministic", fixture_repo, sample_issue, settings, cache=cache)
        reuse = next(
            transition
            for transition in second.state["transitions"]
            if transition.to_state is RunState.INDEX
        )
        assert "reused cached index" in reuse.reason
        assert first.summary.repo_sha == second.summary.repo_sha
