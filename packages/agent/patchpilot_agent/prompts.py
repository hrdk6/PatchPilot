"""Prompt construction.

Two prompts, both deliberately boring and explicit:

* **PLAN** -- the model must emit a JSON object matching :class:`RepairPlan`
  before it is allowed to touch code. A model that cannot say what is wrong does
  not get to guess at a patch.
* **PATCH** -- the model must emit a unified diff and nothing else.

Both carry a ``PatchPilot-Task:`` header line. It documents the step for a human
reading a stored prompt, and it is what the deterministic mock adapter keys off.

The rendered context is exactly what the API returns for the run, so what you see
in the dashboard is what the model saw.
"""

from __future__ import annotations

from patchpilot_core.models import (
    AttemptRecord,
    ChatMessage,
    ContextPackage,
    IssueSpec,
    RepairPlan,
)

TASK_HEADER = "PatchPilot-Task"

PLAN_SYSTEM = """\
You are PatchPilot, a careful software maintenance engineer.

You will be given a bug report and a context package retrieved from the
repository. Produce a repair plan. Do not write code yet.

Rules:
- Reply with a single JSON object and nothing else. No prose, no code fences.
- Use exactly these keys: root_cause, files_to_change, tests_to_run,
  patch_strategy, assumptions, uncertainties, confidence.
- root_cause and patch_strategy are strings of at least 10 characters.
- files_to_change and tests_to_run are arrays of repository-relative paths.
- assumptions and uncertainties are arrays of strings. Be honest in
  uncertainties: if the retrieved context does not show you the real cause, say
  so rather than inventing one.
- confidence is a number between 0 and 1.
- Fix the source, not the test, unless the bug report says the test is wrong.
"""

PATCH_SYSTEM = """\
You are PatchPilot, a careful software maintenance engineer.

Produce the smallest patch that implements the plan.

Rules:
- Reply with a unified diff and nothing else. No prose, no explanation, no code
  fences.
- Use git-style headers: `--- a/path/to/file.py` and `+++ b/path/to/file.py`.
- Include at least 3 lines of unchanged context around each hunk, copied exactly
  from the file contents you were given, including indentation.
- Only modify files shown to you in the context package.
- Do not modify CI configuration, dependency lockfiles, Docker settings or
  hidden files.
- Do not reformat unrelated code and do not add unrelated imports.
- If the fix requires a file you were not shown, emit no diff at all and instead
  reply with the single line: INSUFFICIENT_CONTEXT
"""


def render_context(context: ContextPackage, *, include_conventions: bool = True) -> str:
    """Render the retrieved context package into the exact text the model sees."""
    sections: list[str] = []

    if context.repo_tree_excerpt:
        sections.append("## Repository files\n```\n" + context.repo_tree_excerpt + "\n```")

    if include_conventions and context.conventions:
        convention_blocks = []
        for name, body in context.conventions.items():
            trimmed = body.strip()
            if not trimmed:
                continue
            convention_blocks.append(f"### {name}\n```\n{trimmed[:1200]}\n```")
        if convention_blocks:
            sections.append("## Repository conventions\n" + "\n".join(convention_blocks))

    if context.chunks:
        blocks = ["## Retrieved code"]
        for item in context.chunks:
            chunk = item.chunk
            blocks.append(
                f"### {chunk.path} :: {chunk.symbol} "
                f"({chunk.symbol_type}, lines {chunk.start_line}-{chunk.end_line})\n"
                f"<!-- retrieval score {item.score:.3f}: {item.explain()} -->\n"
                f"```{chunk.language}\n{chunk.content}\n```"
            )
        sections.append("\n".join(blocks))

    if context.failure_excerpt:
        sections.append(
            "## Failing command output\n```\n" + context.failure_excerpt.strip() + "\n```"
        )

    return "\n\n".join(sections)


def _issue_block(issue: IssueSpec) -> str:
    header = "## Bug report"
    if issue.number is not None:
        header += f" (issue #{issue.number})"
    title = issue.title.strip() or "(no title)"
    return f"{header}\n**{title}**\n\n{issue.body.strip()}"


def _history_block(history: list[AttemptRecord]) -> str:
    if not history:
        return ""
    lines = ["## Previous attempts in this run"]
    for record in history:
        lines.append(f"### Attempt {record.attempt}")
        if record.plan_error:
            lines.append(f"- The plan was rejected: {record.plan_error}")
        if record.validation and not record.validation.valid:
            reasons = ", ".join(str(reason) for reason in record.validation.rejections)
            lines.append(f"- The patch was rejected before execution ({reasons}).")
            for message in record.validation.messages[:3]:
                lines.append(f"  - {message}")
        if record.execution is not None:
            excerpt = record.execution.failure_excerpt(1500)
            if excerpt:
                lines.append("- The patch applied but the command still failed:")
                lines.append(f"```\n{excerpt}\n```")
        if record.patch and record.patch.diff:
            lines.append("- The diff that was tried:")
            lines.append(f"```diff\n{record.patch.diff[:2000]}\n```")
        if record.analysis:
            lines.append(f"- Analysis: {record.analysis}")
    lines.append(
        "\nDo not repeat a patch that has already been tried. If the previous "
        "approach was wrong, change the approach."
    )
    return "\n".join(lines)


def build_plan_messages(
    issue: IssueSpec,
    context: ContextPackage,
    *,
    attempt: int,
    history: list[AttemptRecord] | None = None,
) -> list[ChatMessage]:
    parts = [
        f"{TASK_HEADER}: PLAN (attempt {attempt})",
        _issue_block(issue),
        render_context(context),
    ]
    history_block = _history_block(history or [])
    if history_block:
        parts.append(history_block)
    parts.append("Produce the repair plan as a single JSON object now.")
    return [
        ChatMessage(role="system", content=PLAN_SYSTEM),
        ChatMessage(role="user", content="\n\n".join(part for part in parts if part)),
    ]


def build_patch_messages(
    issue: IssueSpec,
    context: ContextPackage,
    plan: RepairPlan,
    *,
    attempt: int,
    history: list[AttemptRecord] | None = None,
) -> list[ChatMessage]:
    plan_block = (
        "## Approved plan\n"
        f"- Root cause: {plan.root_cause}\n"
        f"- Files to change: {', '.join(plan.files_to_change) or '(unspecified)'}\n"
        f"- Tests to run: {', '.join(plan.tests_to_run) or '(unspecified)'}\n"
        f"- Strategy: {plan.patch_strategy}\n"
        f"- Assumptions: {'; '.join(plan.assumptions) or 'none stated'}\n"
        f"- Uncertainties: {'; '.join(plan.uncertainties) or 'none stated'}"
    )
    parts = [
        f"{TASK_HEADER}: GENERATE_PATCH (attempt {attempt})",
        _issue_block(issue),
        render_context(context),
        plan_block,
    ]
    history_block = _history_block(history or [])
    if history_block:
        parts.append(history_block)
    parts.append("Produce the unified diff now.")
    return [
        ChatMessage(role="system", content=PATCH_SYSTEM),
        ChatMessage(role="user", content="\n\n".join(part for part in parts if part)),
    ]


def estimate_prompt_chars(messages: list[ChatMessage]) -> int:
    return sum(len(message.content) for message in messages)
