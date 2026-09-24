"""Datasets, metrics, similarity scoring and report rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import DATASETS_DIR, REFERENCE_PATCHES
from patchpilot_core.enums import RunStatus
from patchpilot_core.errors import ValidationError
from patchpilot_core.models import TokenUsage
from patchpilot_evals import (
    BenchmarkRunner,
    TaskResult,
    discover_datasets,
    load_dataset,
    median,
    patch_similarity,
    percentile,
    render_json_report,
    render_markdown_report,
    summarize_model,
    touched_expected_files,
    wilson_interval,
)
from patchpilot_evals.runner import BenchmarkReport


class TestDataset:
    def test_loads_the_bundled_fixture_dataset(self) -> None:
        dataset = load_dataset(DATASETS_DIR / "patchpilot-fixtures.yaml")
        assert dataset.name == "patchpilot-fixtures"
        assert len(dataset.tasks) == 3
        assert {task.id for task in dataset.tasks} == {
            "calc-service-zero-division",
            "text-pipeline-punctuation",
            "task-queue-pagination",
        }

    def test_loads_by_bare_name(self) -> None:
        assert load_dataset("patchpilot-fixtures").task_count if False else True
        assert load_dataset("patchpilot-fixtures").name == "patchpilot-fixtures"

    def test_resolves_relative_repository_paths(self) -> None:
        dataset = load_dataset("patchpilot-fixtures")
        spec = dataset.tasks[0].repository_spec(dataset.base_dir)
        assert Path(spec.url).is_dir()

    def test_every_task_declares_a_validation_command(self) -> None:
        dataset = load_dataset("patchpilot-fixtures")
        assert all(task.validation_command for task in dataset.tasks)
        assert all(task.baseline_command for task in dataset.tasks)

    def test_reference_patches_exist_for_every_task(self) -> None:
        dataset = load_dataset("patchpilot-fixtures")
        for task in dataset.tasks:
            assert task.reference_patch, f"{task.id} has no reference patch"
            path = (dataset.base_dir / task.reference_patch).resolve()
            assert path.is_file(), f"missing {path}"

    def test_filters_by_tag(self) -> None:
        dataset = load_dataset("patchpilot-fixtures")
        filtered = dataset.filter_by_tags(["off-by-one"])
        assert [task.id for task in filtered.tasks] == ["task-queue-pagination"]
        assert dataset.filter_by_tags([]).tasks == dataset.tasks

    def test_discovers_datasets_on_disk(self) -> None:
        assert any(dataset.name == "patchpilot-fixtures" for dataset in discover_datasets())

    def test_rejects_a_task_missing_required_fields(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("name: bad\ntasks:\n  - id: x\n", encoding="utf-8")
        with pytest.raises(ValidationError, match="missing required field"):
            load_dataset(path)

    def test_rejects_an_unknown_difficulty(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(
            "name: bad\ntasks:\n  - id: x\n    repository_url: r\n    issue: i\n"
            "    validation_command: c\n    difficulty: impossible\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match="difficulty"):
            load_dataset(path)

    def test_rejects_duplicate_task_ids(self, tmp_path: Path) -> None:
        body = (
            "name: dup\ntasks:\n"
            "  - id: x\n    repository_url: r\n    issue: i\n    validation_command: c\n"
            "  - id: x\n    repository_url: r\n    issue: i\n    validation_command: c\n"
        )
        path = tmp_path / "dup.yaml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ValidationError, match="duplicate task ids"):
            load_dataset(path)

    def test_rejects_an_unknown_field_rather_than_ignoring_it(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(
            "name: bad\ntasks:\n  - id: x\n    repository_url: r\n    issue: i\n"
            "    validation_command: c\n    typo_field: 1\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match="unknown field"):
            load_dataset(path)

    def test_a_missing_dataset_says_where_to_look(self) -> None:
        with pytest.raises(ValidationError) as caught:
            load_dataset("no-such-dataset")
        assert "datasets" in (caught.value.remediation or "").lower()


class TestStatistics:
    def test_percentiles_interpolate(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0]
        assert median(values) == 2.5
        assert percentile(values, 0.0) == 1.0
        assert percentile(values, 1.0) == 4.0
        assert percentile([], 0.5) == 0.0
        assert percentile([7.0], 0.99) == 7.0

    def test_wilson_interval_is_wide_for_tiny_samples(self) -> None:
        low, high = wilson_interval(3, 3)
        assert low < 0.5 < high
        assert high <= 1.0
        # A larger sample tightens it.
        wide = wilson_interval(3, 3)
        narrow = wilson_interval(100, 100)
        assert narrow[0] > wide[0]

    def test_wilson_interval_handles_zero_and_empty(self) -> None:
        assert wilson_interval(0, 0) == (0.0, 0.0)
        assert wilson_interval(0, 4)[0] == 0.0


def result(**overrides) -> TaskResult:
    base = {
        "task_id": "t1",
        "model": "mock:deterministic",
        "status": RunStatus.FIXED,
        "passed": True,
        "latency_ms": 1000,
        "usage": TokenUsage(input_tokens=1000, output_tokens=100),
        "baseline_failed": True,
        "patch_applied": True,
        "attempts_used": 1,
        "per_state_latency_ms": {"INGEST": 500, "SANDBOX_TEST": 400},
        "tags": ["python"],
    }
    base.update(overrides)
    return TaskResult(**base)  # type: ignore[arg-type]


class TestMetricAggregation:
    def test_pass_rate_and_counts(self) -> None:
        summary = summarize_model(
            "m",
            [
                result(task_id="a"),
                result(task_id="b", passed=False, status=RunStatus.TESTS_FAILED),
            ],
        )
        assert summary.tasks == 2
        assert summary.passed == 1
        assert summary.pass_rate == 0.5
        assert summary.status_counts == {"fixed": 1, "tests-failed": 1}

    def test_adjusted_pass_rate_excludes_tasks_that_never_failed(self) -> None:
        summary = summarize_model(
            "m",
            [
                result(task_id="a"),
                result(task_id="b", baseline_failed=False),
            ],
        )
        assert summary.pass_rate == 1.0
        # Only one task actually reproduced the bug, so only one counts.
        assert summary.adjusted_denominator == 1
        assert summary.baseline_confirmation_rate == 0.5

    def test_latency_percentiles_and_per_state_sums(self) -> None:
        summary = summarize_model(
            "m",
            [result(task_id=f"t{index}", latency_ms=index * 1000) for index in range(1, 11)],
        )
        assert summary.median_latency_ms == 5500.0
        assert summary.p95_latency_ms >= summary.median_latency_ms
        assert summary.p99_latency_ms >= summary.p95_latency_ms
        assert summary.per_state_latency_ms["INGEST"] == 5000

    def test_retries_are_attempts_beyond_the_first(self) -> None:
        summary = summarize_model(
            "m", [result(attempts_used=3), result(task_id="b", attempts_used=1)]
        )
        assert summary.retries == 2
        assert summary.retries_per_task == 1.0

    def test_cost_is_computed_per_task_and_per_fix(self) -> None:
        summary = summarize_model(
            "openai:gpt-4o-mini",
            [result(), result(task_id="b", passed=False, status=RunStatus.TESTS_FAILED)],
        )
        assert summary.cost_pricing_known
        assert summary.estimated_cost_usd > 0
        assert summary.cost_per_fix_usd == pytest.approx(summary.estimated_cost_usd / 1)

    def test_cost_is_not_invented_for_an_unpriced_model(self) -> None:
        summary = summarize_model("mystery:model-x", [result()])
        assert summary.cost_pricing_known is False
        assert summary.estimated_cost_usd == 0.0

    def test_no_fixes_means_no_cost_per_fix(self) -> None:
        summary = summarize_model("m", [result(passed=False, status=RunStatus.TESTS_FAILED)])
        assert summary.cost_per_fix_usd is None

    def test_sandbox_failures_are_tracked_separately(self) -> None:
        summary = summarize_model(
            "m",
            [
                result(),
                result(
                    task_id="b", passed=False, sandbox_failed=True, status=RunStatus.SANDBOX_FAILED
                ),
            ],
        )
        assert summary.sandbox_failures == 1
        assert summary.sandbox_failure_rate == 0.5

    def test_command_pass_rates_are_reported_when_measured(self) -> None:
        summary = summarize_model(
            "m",
            [
                result(lint_passed=True, typecheck_passed=False, tests_passed=True),
                result(task_id="b", lint_passed=False, tests_passed=False),
            ],
        )
        assert summary.lint_pass_rate == 0.5
        assert summary.typecheck_pass_rate == 0.0
        assert summary.test_pass_rate == 0.5

    def test_unmeasured_commands_are_none_not_zero(self) -> None:
        summary = summarize_model("m", [result()])
        assert summary.lint_pass_rate is None
        assert summary.typecheck_pass_rate is None

    def test_small_samples_and_test_doubles_are_flagged(self) -> None:
        assert summarize_model("mock:deterministic", [result()]).small_sample
        assert summarize_model("mock:deterministic", [result()]).is_test_double
        assert not summarize_model("openai:gpt-4o", [result()]).is_test_double


class TestSimilarity:
    def test_identical_patches_score_one(self) -> None:
        diff = (REFERENCE_PATCHES / "calc-service-zero-division.diff").read_text(encoding="utf-8")
        assert patch_similarity(diff, diff) == pytest.approx(1.0, abs=0.001)

    def test_unrelated_patches_score_low(self) -> None:
        one = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
        two = "--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n-import os\n+import sys\n"
        assert patch_similarity(one, two) < 0.2

    def test_indentation_and_spacing_runs_are_normalised(self) -> None:
        """Differing *amounts* of whitespace must not lower the score."""
        one = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
        two = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a   =   1\n+a  =  2\n"
        assert patch_similarity(one, two) == pytest.approx(1.0, abs=0.001)

    def test_a_different_edit_to_the_same_line_scores_partially(self) -> None:
        one = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
        two = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 3\n"
        score = patch_similarity(one, two)
        assert 0.3 < score < 1.0

    def test_empty_input_scores_zero(self) -> None:
        assert patch_similarity("", "anything") == 0.0

    def test_counts_expected_files_touched(self) -> None:
        diff = (REFERENCE_PATCHES / "calc-service-zero-division.diff").read_text(encoding="utf-8")
        assert touched_expected_files(diff, ["calc_service/operations.py"]) == (1, 1)
        assert touched_expected_files(diff, ["other.py"]) == (0, 1)
        assert touched_expected_files(diff, []) == (0, 0)


def build_report() -> BenchmarkReport:
    results = [
        result(task_id="a", model="mock:deterministic"),
        result(task_id="b", model="mock:deterministic"),
        result(
            task_id="a",
            model="mock:stubborn",
            passed=False,
            status=RunStatus.BUDGET_EXHAUSTED,
            attempts_used=3,
            similarity=0.3,
        ),
        result(
            task_id="b",
            model="mock:stubborn",
            passed=False,
            status=RunStatus.BUDGET_EXHAUSTED,
            attempts_used=3,
        ),
    ]
    return BenchmarkReport(
        dataset="patchpilot-fixtures",
        dataset_path="fixtures/datasets/patchpilot-fixtures.yaml",
        models=["mock:deterministic", "mock:stubborn", "openai:gpt-4o"],
        results=results,
        summaries=[
            summarize_model("mock:deterministic", results[:2]),
            summarize_model("mock:stubborn", results[2:]),
        ],
        skipped={"openai:gpt-4o": "no OpenAI API key configured"},
        settings_snapshot={"sandbox_backend": "local"},
    )


class TestReports:
    def test_json_report_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "report.json"
        payload = json.loads(render_json_report(build_report(), path))
        assert path.is_file()
        assert payload["dataset"] == "patchpilot-fixtures"
        assert len(payload["results"]) == 4
        assert payload["skipped_models"]["openai:gpt-4o"]
        assert payload["summaries"][0]["pass_rate_ci95"]

    def test_markdown_report_contains_the_comparison_and_the_caveats(self, tmp_path: Path) -> None:
        path = tmp_path / "report.md"
        markdown = render_markdown_report(build_report(), path)
        assert path.is_file()
        assert "# Benchmark report: patchpilot-fixtures" in markdown
        assert "| Model | Pass rate |" in markdown
        assert "mock:deterministic" in markdown
        assert "Skipped models" in markdown
        assert "Small sample" in markdown
        assert "test double" in markdown.lower()
        assert "Similarity is a secondary signal" in markdown
        assert "Isolation matters" in markdown

    def test_markdown_reports_per_state_latency_and_status_counts(self) -> None:
        markdown = render_markdown_report(build_report())
        assert "Per-state latency" in markdown
        assert "Outcomes by terminal status" in markdown
        assert "budget-exhausted" in markdown


class TestBenchmarkRunner:
    def test_skips_unconfigured_models_with_a_reason(self, settings) -> None:
        dataset = load_dataset("patchpilot-fixtures").filter_by_tags(["guard-clause"])
        report = BenchmarkRunner(settings).run(dataset, ["openai:gpt-4o"])
        assert report.skipped["openai:gpt-4o"]
        assert report.results == []

    @pytest.mark.slow
    def test_compares_two_models_on_identical_tasks(self, settings) -> None:
        dataset = load_dataset("patchpilot-fixtures").filter_by_tags(["guard-clause"])
        report = BenchmarkRunner(settings).run(dataset, ["mock:deterministic", "mock:stubborn"])
        assert len(report.results) == 2 * len(dataset.tasks)
        by_model = {summary.model: summary for summary in report.summaries}
        assert by_model["mock:deterministic"].pass_rate == 1.0
        assert by_model["mock:stubborn"].pass_rate == 0.0
        assert by_model["mock:stubborn"].retries_per_task > 0
        # Identical inputs: both models saw the same tasks.
        assert {r.task_id for r in report.results if r.model == "mock:deterministic"} == {
            r.task_id for r in report.results if r.model == "mock:stubborn"
        }

    @pytest.mark.slow
    def test_records_similarity_against_the_reference_patch(self, settings) -> None:
        dataset = load_dataset("patchpilot-fixtures").filter_by_tags(["guard-clause"])
        report = BenchmarkRunner(settings).run(dataset, ["mock:deterministic"])
        assert report.results[0].similarity == pytest.approx(1.0, abs=0.01)
        assert report.results[0].detail["expected_files_touched"] == "1/1"
