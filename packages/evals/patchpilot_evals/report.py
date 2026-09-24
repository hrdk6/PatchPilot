"""Report rendering: machine-readable JSON and a readable Markdown summary.

The Markdown report is written to be pasted into a pull request or a design doc,
which means it has to be honest without a human editing it first: it states the
sample size, the confidence interval, whether the sandbox was actually isolated,
whether cost is estimated, and which models are test doubles.
"""

from __future__ import annotations

import json
from pathlib import Path

from .metrics import SMALL_SAMPLE_THRESHOLD, ModelSummary, wilson_interval
from .runner import BenchmarkReport


def render_json_report(report: BenchmarkReport, path: Path | None = None) -> str:
    text = json.dumps(report.to_dict(), indent=2, sort_keys=False)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return text


def _percent(value: float) -> str:
    return f"{value * 100:.0f}%"


def _ms(value: float) -> str:
    if value >= 1000:
        return f"{value / 1000:.1f}s"
    return f"{value:.0f}ms"


def _cost(summary: ModelSummary) -> str:
    if not summary.cost_pricing_known:
        return "n/a"
    if summary.estimated_cost_usd == 0:
        return "$0.00"
    return f"${summary.estimated_cost_usd:.4f}"


def render_markdown_report(report: BenchmarkReport, path: Path | None = None) -> str:
    lines: list[str] = []
    lines.append(f"# Benchmark report: {report.dataset}")
    lines.append("")
    lines.append(f"- Dataset: `{report.dataset_path}`")
    lines.append(f"- Started: {report.started_at.isoformat(timespec='seconds')}")
    if report.finished_at:
        lines.append(f"- Finished: {report.finished_at.isoformat(timespec='seconds')}")
    lines.append(f"- Models compared: {len(report.summaries)}")
    tasks_per_model = report.summaries[0].tasks if report.summaries else 0
    lines.append(f"- Tasks per model: {tasks_per_model}")
    lines.append("")

    environment = report.settings_snapshot
    if environment:
        lines.append("## Environment")
        lines.append("")
        for key, value in environment.items():
            lines.append(f"- `{key}`: {value}")
        lines.append("")

    if report.skipped:
        lines.append("## Skipped models")
        lines.append("")
        for model, reason in report.skipped.items():
            lines.append(f"- `{model}`: {reason}")
        lines.append("")

    lines.append("## Comparison")
    lines.append("")
    lines.append(
        "| Model | Pass rate | 95% CI | Adjusted | Patch applied | Median | p95 | p99 | "
        "Retries/task | Tokens | Est. cost | Sandbox failures |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for summary in report.summaries:
        low, high = wilson_interval(summary.passed, summary.tasks)
        label = summary.model + (" *(test double)*" if summary.is_test_double else "")
        lines.append(
            f"| `{label}` | {_percent(summary.pass_rate)} "
            f"({summary.passed}/{summary.tasks}) "
            f"| {_percent(low)} to {_percent(high)} "
            f"| {_percent(summary.adjusted_pass_rate)} "
            f"({summary.adjusted_denominator} confirmed) "
            f"| {_percent(summary.patch_application_rate)} "
            f"| {_ms(summary.median_latency_ms)} "
            f"| {_ms(summary.p95_latency_ms)} "
            f"| {_ms(summary.p99_latency_ms)} "
            f"| {summary.retries_per_task:.2f} "
            f"| {summary.total_tokens:,} "
            f"| {_cost(summary)} "
            f"| {summary.sandbox_failures} |"
        )
    lines.append("")

    lines.append("## Per-state latency (summed across tasks)")
    lines.append("")
    states = sorted({state for s in report.summaries for state in s.per_state_latency_ms})
    if states:
        lines.append("| Model | " + " | ".join(states) + " |")
        lines.append("| --- |" + " --- |" * len(states))
        for summary in report.summaries:
            cells = [_ms(summary.per_state_latency_ms.get(state, 0)) for state in states]
            lines.append(f"| `{summary.model}` | " + " | ".join(cells) + " |")
        lines.append("")

    lines.append("## Outcomes by terminal status")
    lines.append("")
    all_statuses = sorted({key for s in report.summaries for key in s.status_counts})
    if all_statuses:
        lines.append("| Model | " + " | ".join(all_statuses) + " |")
        lines.append("| --- |" + " --- |" * len(all_statuses))
        for summary in report.summaries:
            cells = [str(summary.status_counts.get(status, 0)) for status in all_statuses]
            lines.append(f"| `{summary.model}` | " + " | ".join(cells) + " |")
        lines.append("")

    lines.append("## Per-task results")
    lines.append("")
    lines.append("| Task | Model | Status | Attempts | Latency | Tokens | Similarity |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for result in report.results:
        similarity = "—" if result.similarity is None else f"{result.similarity:.2f}"
        lines.append(
            f"| `{result.task_id}` | `{result.model}` | {result.status} "
            f"| {result.attempts_used} | {_ms(result.latency_ms)} "
            f"| {result.usage.total_tokens:,} | {similarity} |"
        )
    lines.append("")

    lines.append("## How to read this")
    lines.append("")
    lines.extend(_confidence_notes(report))
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
    return "\n".join(lines)


def _confidence_notes(report: BenchmarkReport) -> list[str]:
    notes: list[str] = []
    small = [s for s in report.summaries if s.small_sample]
    if small:
        smallest = min(s.tasks for s in small)
        notes.append(
            f"- **Small sample.** {len(small)} model(s) were evaluated on {smallest} "
            f"task(s), below the {SMALL_SAMPLE_THRESHOLD}-task threshold at which these "
            "rates start to mean much. Read the confidence interval, not the point "
            "estimate: a 3-for-3 result is consistent with a true pass rate near 40%."
        )
    if any(s.is_test_double for s in report.summaries):
        notes.append(
            "- **Test doubles included.** Models prefixed `mock:` replay a scripted "
            "patch. Their pass rate measures whether the harness works, not whether a "
            "model can reason; do not compare them against real models."
        )
    unconfirmed = [s for s in report.summaries if s.baseline_confirmation_rate < 1.0]
    if unconfirmed:
        notes.append(
            "- **Baseline confirmation.** Some tasks did not fail before patching, so "
            "a pass there proves nothing about repair. The *Adjusted* column excludes "
            "them."
        )
    if any(not s.cost_pricing_known for s in report.summaries):
        notes.append(
            "- **Cost unavailable** for at least one model: no pricing entry. Token "
            "counts are still exact where the provider reported them."
        )
    else:
        notes.append(
            "- **Cost is estimated** from a static pricing snapshot and the reported "
            "token counts. Verify against your provider's invoice before quoting it."
        )
    notes.append(
        "- **Similarity is a secondary signal.** It measures textual overlap with a "
        "reference patch. A correct fix can score low and an incorrect one can score "
        "high; only the sandboxed validation command decides pass or fail."
    )
    notes.append(
        "- **Isolation matters.** Check `sandbox_backend` in the environment block. "
        "Results produced with the `local` backend ran without isolation and are only "
        "meaningful for the trusted fixture repositories."
    )
    return notes
