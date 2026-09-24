"""Metric definitions and aggregation.

Every number the benchmark reports is defined here exactly once, so the JSON, the
Markdown report and the dashboard cannot disagree about what "pass rate" means.

Definitions (also in docs/evaluation.md):

* **pass rate** -- tasks whose final status is ``fixed`` / tasks attempted.
* **baseline failure confirmation** -- tasks where the unpatched checkout
  actually failed the baseline command. A task that passes before any patch is
  not measuring repair, and is excluded from ``adjusted_pass_rate``.
* **patch application rate** -- tasks where at least one proposed patch survived
  validation and applied in the sandbox.
* **lint/type/test pass rates** -- per-command outcomes on the final attempt.
* **latency** -- end-to-end wall clock per task, reported as median/p95/p99, plus
  a per-state breakdown summed across tasks.
* **retries** -- attempts used beyond the first.
* **sandbox failure rate** -- tasks that ended with an unusable sandbox; these
  measure the harness, not the model.
* **similarity** -- optional, secondary, and never treated as correctness.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from patchpilot_core.costs import estimate_cost
from patchpilot_core.enums import RunStatus
from patchpilot_core.models import TokenUsage

# Below this many tasks per model the numbers are indicative only; the report
# says so rather than quietly presenting a 1-of-3 result as a percentage.
SMALL_SAMPLE_THRESHOLD = 10


def percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolation percentile. ``fraction`` is 0..1."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[int(position)])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def median(values: list[float]) -> float:
    return percentile(values, 0.5)


def rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


@dataclass(slots=True)
class TaskResult:
    """One (task, model) pair."""

    task_id: str
    model: str
    status: RunStatus
    passed: bool
    latency_ms: int
    usage: TokenUsage = field(default_factory=TokenUsage)
    run_id: str | None = None
    baseline_failed: bool | None = None
    patch_applied: bool = False
    attempts_used: int = 0
    sandbox_failed: bool = False
    lint_passed: bool | None = None
    typecheck_passed: bool | None = None
    tests_passed: bool | None = None
    similarity: float | None = None
    per_state_latency_ms: dict[str, int] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        cost = estimate_cost(self.model, self.usage)
        return {
            "task_id": self.task_id,
            "model": self.model,
            "run_id": self.run_id,
            "status": str(self.status),
            "passed": self.passed,
            "baseline_failed": self.baseline_failed,
            "patch_applied": self.patch_applied,
            "attempts_used": self.attempts_used,
            "latency_ms": self.latency_ms,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "total_tokens": self.usage.total_tokens,
            "estimated_cost_usd": cost.total_usd,
            "cost_pricing_known": cost.pricing_known,
            "sandbox_failed": self.sandbox_failed,
            "lint_passed": self.lint_passed,
            "typecheck_passed": self.typecheck_passed,
            "tests_passed": self.tests_passed,
            "similarity": self.similarity,
            "per_state_latency_ms": self.per_state_latency_ms,
            "tags": list(self.tags),
            "detail": self.detail,
        }


@dataclass(slots=True)
class ModelSummary:
    model: str
    tasks: int
    passed: int
    pass_rate: float
    adjusted_pass_rate: float
    adjusted_denominator: int
    baseline_confirmed: int
    baseline_confirmation_rate: float
    patch_applied: int
    patch_application_rate: float
    lint_pass_rate: float | None
    typecheck_pass_rate: float | None
    test_pass_rate: float
    median_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    total_latency_ms: int
    per_state_latency_ms: dict[str, int]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    cost_pricing_known: bool
    cost_per_task_usd: float
    cost_per_fix_usd: float | None
    retries: int
    retries_per_task: float
    sandbox_failures: int
    sandbox_failure_rate: float
    mean_similarity: float | None
    status_counts: dict[str, int]
    small_sample: bool
    is_test_double: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            key: getattr(self, key)
            for key in self.__dataclass_fields__  # type: ignore[attr-defined]
        }


def summarize_model(model: str, results: list[TaskResult]) -> ModelSummary:
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    confirmed = [result for result in results if result.baseline_failed]
    confirmed_passed = sum(1 for result in confirmed if result.passed)
    latencies = [float(result.latency_ms) for result in results]
    usage = TokenUsage()
    for result in results:
        usage = usage + result.usage
    cost = estimate_cost(model, usage)

    per_state: dict[str, int] = {}
    for result in results:
        for state, value in result.per_state_latency_ms.items():
            per_state[state] = per_state.get(state, 0) + value

    lint_values = [r.lint_passed for r in results if r.lint_passed is not None]
    type_values = [r.typecheck_passed for r in results if r.typecheck_passed is not None]
    test_values = [r.tests_passed for r in results if r.tests_passed is not None]
    similarities = [r.similarity for r in results if r.similarity is not None]
    retries = sum(max(result.attempts_used - 1, 0) for result in results)
    sandbox_failures = sum(1 for result in results if result.sandbox_failed)

    status_counts: dict[str, int] = {}
    for result in results:
        key = str(result.status)
        status_counts[key] = status_counts.get(key, 0) + 1

    return ModelSummary(
        model=model,
        tasks=total,
        passed=passed,
        pass_rate=rate(passed, total),
        adjusted_pass_rate=rate(confirmed_passed, len(confirmed)),
        adjusted_denominator=len(confirmed),
        baseline_confirmed=len(confirmed),
        baseline_confirmation_rate=rate(len(confirmed), total),
        patch_applied=sum(1 for result in results if result.patch_applied),
        patch_application_rate=rate(sum(1 for result in results if result.patch_applied), total),
        lint_pass_rate=rate(sum(1 for value in lint_values if value), len(lint_values))
        if lint_values
        else None,
        typecheck_pass_rate=rate(sum(1 for value in type_values if value), len(type_values))
        if type_values
        else None,
        test_pass_rate=rate(sum(1 for value in test_values if value), len(test_values))
        if test_values
        else 0.0,
        median_latency_ms=round(median(latencies), 1),
        p95_latency_ms=round(percentile(latencies, 0.95), 1),
        p99_latency_ms=round(percentile(latencies, 0.99), 1),
        total_latency_ms=int(sum(latencies)),
        per_state_latency_ms=per_state,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        estimated_cost_usd=cost.total_usd,
        cost_pricing_known=cost.pricing_known,
        cost_per_task_usd=round(cost.total_usd / total, 6) if total else 0.0,
        cost_per_fix_usd=round(cost.total_usd / passed, 6) if passed else None,
        retries=retries,
        retries_per_task=round(retries / total, 3) if total else 0.0,
        sandbox_failures=sandbox_failures,
        sandbox_failure_rate=rate(sandbox_failures, total),
        mean_similarity=round(sum(similarities) / len(similarities), 4) if similarities else None,
        status_counts=status_counts,
        small_sample=total < SMALL_SAMPLE_THRESHOLD,
        is_test_double=model.startswith("mock:"),
    )


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval, which behaves sensibly at tiny sample sizes.

    Reported alongside every pass rate so a 3-task benchmark is not mistaken for
    evidence of a 100% success rate.
    """
    if total == 0:
        return (0.0, 0.0)
    proportion = successes / total
    denominator = 1 + z**2 / total
    centre = proportion + z**2 / (2 * total)
    spread = z * math.sqrt(proportion * (1 - proportion) / total + z**2 / (4 * total**2))
    lower = (centre - spread) / denominator
    upper = (centre + spread) / denominator
    return (round(max(0.0, lower), 4), round(min(1.0, upper), 4))
