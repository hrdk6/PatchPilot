"""The benchmark runner: the same task set against one or many models.

Every model sees the identical repository snapshot, the identical issue text and
the identical commands. The only variable is the model. That is what makes the
comparison mean anything.

A model that is not configured (no API key) is skipped with a recorded reason
rather than silently omitted -- an empty column in a comparison table is a
finding, not a blank.
"""

from __future__ import annotations

import time
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from patchpilot_agent import build_adapter, run_agent
from patchpilot_core.config import Settings, get_settings
from patchpilot_core.enums import CommandKind, RunStatus, SandboxStatus
from patchpilot_core.errors import ConfigurationError, PatchPilotError
from patchpilot_core.ids import new_id
from patchpilot_core.logging import get_logger, log_context
from patchpilot_core.models import RunConfig, TokenUsage
from patchpilot_indexer import RepositoryIndex

from .dataset import BenchmarkTask, Dataset
from .metrics import ModelSummary, TaskResult, summarize_model, wilson_interval
from .similarity import patch_similarity, touched_expected_files

logger = get_logger(__name__, component="evals")


@dataclass(slots=True)
class BenchmarkReport:
    dataset: str
    dataset_path: str
    models: list[str]
    results: list[TaskResult] = field(default_factory=list)
    summaries: list[ModelSummary] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    settings_snapshot: dict[str, Any] = field(default_factory=dict)
    benchmark_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "dataset": self.dataset,
            "dataset_path": self.dataset_path,
            "models": list(self.models),
            "skipped_models": self.skipped,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "environment": self.settings_snapshot,
            "summaries": [
                {
                    **summary.to_dict(),
                    "pass_rate_ci95": wilson_interval(summary.passed, summary.tasks),
                }
                for summary in self.summaries
            ],
            "results": [result.to_dict() for result in self.results],
        }


class BenchmarkRunner:
    """Executes a dataset against a list of model identifiers."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        index_cache: MutableMapping[str, RepositoryIndex] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        # Sharing the index cache across tasks is safe -- it is keyed by repo SHA --
        # and means model B is not charged for indexing that model A already did.
        self.index_cache = index_cache if index_cache is not None else {}

    def run(
        self,
        dataset: Dataset,
        models: list[str],
        *,
        benchmark_id: str = "",
        progress: Any = None,
    ) -> BenchmarkReport:
        report = BenchmarkReport(
            dataset=dataset.name,
            dataset_path=str(dataset.path),
            models=list(models),
            benchmark_id=benchmark_id or new_id("bench"),
            settings_snapshot=self._environment(),
        )

        usable: list[str] = []
        for model in models:
            try:
                build_adapter(model, self.settings)
            except ConfigurationError as exc:
                report.skipped[model] = exc.message
                logger.warning("skipping model", extra={"model": model, "reason": exc.message})
                continue
            usable.append(model)

        for model in usable:
            for task in dataset.tasks:
                result = self._run_task(dataset, task, model)
                report.results.append(result)
                if progress is not None:
                    progress(result)

        for model in usable:
            model_results = [r for r in report.results if r.model == model]
            if model_results:
                report.summaries.append(summarize_model(model, model_results))

        report.finished_at = datetime.now(UTC)
        return report

    # ------------------------------------------------------------------ task
    def _run_task(self, dataset: Dataset, task: BenchmarkTask, model: str) -> TaskResult:
        started = time.perf_counter()
        run_identifier = new_id("run")
        config = RunConfig(
            model=model,
            max_repair_attempts=task.max_repair_attempts or self.settings.max_repair_attempts,
            setup_command=task.setup_command,
            baseline_command=task.baseline_command,
            validation_command=task.validation_command,
            lint_command=task.lint_command,
            typecheck_command=task.typecheck_command,
            retrieval_top_k=self.settings.retrieval_top_k,
            sandbox_backend=self.settings.sandbox_backend,
            limits=self.settings.sandbox_limits(timeout_seconds=task.timeout_seconds),
        )

        with log_context(benchmark_task=task.id, model=model, run_id=run_identifier):
            try:
                outcome = run_agent(
                    repository=task.repository_spec(dataset.base_dir),
                    issue=task.issue_spec(),
                    config=config,
                    settings=self.settings,
                    run_id=run_identifier,
                    index_cache=self.index_cache,
                )
            except PatchPilotError as exc:
                return TaskResult(
                    task_id=task.id,
                    model=model,
                    status=RunStatus.ERROR,
                    passed=False,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    run_id=run_identifier,
                    tags=list(task.tags),
                    detail={"error": exc.message, "code": exc.code},
                )

        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._to_result(task, model, outcome, latency_ms, run_identifier, dataset)

    def _to_result(
        self,
        task: BenchmarkTask,
        model: str,
        outcome,
        latency_ms: int,
        run_identifier: str,
        dataset: Dataset,
    ) -> TaskResult:
        state = outcome.state
        summary = outcome.summary
        attempts = state.get("attempts", [])
        last = attempts[-1] if attempts else None

        patch_applied = any(
            record.validation is not None and record.validation.valid for record in attempts
        )
        sandbox_failed = summary.status is RunStatus.SANDBOX_FAILED or any(
            record.execution is not None
            and record.execution.status
            in (SandboxStatus.UNAVAILABLE, SandboxStatus.SETUP_FAILED, SandboxStatus.INTERNAL_ERROR)
            for record in attempts
        )

        lint_passed = typecheck_passed = tests_passed = None
        if last is not None and last.execution is not None:
            for kind, setter in (
                (CommandKind.LINT, "lint"),
                (CommandKind.TYPECHECK, "typecheck"),
                (CommandKind.VALIDATION, "tests"),
            ):
                result = last.execution.by_kind(kind)
                if result is None:
                    continue
                if setter == "lint":
                    lint_passed = result.passed
                elif setter == "typecheck":
                    typecheck_passed = result.passed
                else:
                    tests_passed = result.passed

        similarity = None
        expected_hit = expected_total = 0
        final_diff = summary.final_patch.diff if summary.final_patch else ""
        if final_diff:
            expected_hit, expected_total = touched_expected_files(final_diff, task.expected_files)
            reference = self._reference_patch(dataset, task)
            if reference:
                similarity = patch_similarity(final_diff, reference)

        return TaskResult(
            task_id=task.id,
            model=model,
            status=summary.status,
            passed=summary.status is RunStatus.FIXED,
            latency_ms=latency_ms,
            usage=summary.usage or TokenUsage(),
            run_id=run_identifier,
            baseline_failed=state.get("baseline_reproduced"),
            patch_applied=patch_applied,
            attempts_used=summary.attempts_used,
            sandbox_failed=sandbox_failed,
            lint_passed=lint_passed,
            typecheck_passed=typecheck_passed,
            tests_passed=tests_passed,
            similarity=similarity,
            per_state_latency_ms=dict(summary.latency.per_state_ms) if summary.latency else {},
            tags=list(task.tags),
            detail={
                "stop_reason": str(summary.stop_reason) if summary.stop_reason else None,
                "expected_files_touched": f"{expected_hit}/{expected_total}"
                if expected_total
                else None,
                "sandbox_backend": state.get("sandbox_backend"),
                "sandbox_isolated": state.get("sandbox_isolated"),
                "error": summary.error,
                "final_patch": final_diff[:8000] or None,
            },
        )

    @staticmethod
    def _reference_patch(dataset: Dataset, task: BenchmarkTask) -> str | None:
        if not task.reference_patch:
            return None
        candidate = Path(task.reference_patch)
        if not candidate.is_absolute():
            candidate = dataset.base_dir / candidate
        if not candidate.is_file():
            logger.warning(
                "reference patch not found", extra={"path": str(candidate), "task": task.id}
            )
            return None
        return candidate.read_text(encoding="utf-8")

    def _environment(self) -> dict[str, Any]:
        return {
            "embedding_provider": self.settings.embedding_provider,
            "embedding_dim": self.settings.embedding_dim,
            "vector_store": "qdrant" if self.settings.qdrant_url else "local",
            "sandbox_backend": self.settings.sandbox_backend,
            "sandbox_image": self.settings.sandbox_image,
            "sandbox_network": self.settings.sandbox_network,
            "max_repair_attempts": self.settings.max_repair_attempts,
            "retrieval_top_k": self.settings.retrieval_top_k,
        }
