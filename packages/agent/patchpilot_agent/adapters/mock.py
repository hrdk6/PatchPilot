"""Deterministic mock adapters: test doubles, not models.

These adapters do **no reasoning**. They replay scripted edits declared in
``fixtures/solutions/*.yaml`` so that CI, the integration tests and the offline
demo can drive the entire pipeline -- retrieval, planning, patch validation,
sandbox execution, the repair loop, persistence and the benchmark harness --
with no network access and no API key.

Five variants exist, each one there to make a specific terminal state
reachable in a test:

======================  =====================================================
``mock:deterministic``  replays the reference edit; the run should end ``fixed``
``mock:stubborn``       patches that apply but never fix; ends ``budget-exhausted``
``mock:flaky``          a malformed diff first, the real fix second
``mock:broken``         prose instead of a diff; ends ``patch-invalid``
``mock:unsafe``         edits a CI workflow; rejected by the patch policy
======================  =====================================================

Benchmark reports label every mock model as a test double, because a pass rate
against a scripted patch measures the harness, not a model.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from patchpilot_core.diffutil import make_unified_diff
from patchpilot_core.logging import get_logger
from patchpilot_core.models import ChatMessage, LLMResponse, TokenUsage
from patchpilot_core.textutil import estimate_tokens

logger = get_logger(__name__, component="adapter", provider="mock")

MOCK_VARIANTS = ("deterministic", "stubborn", "flaky", "broken", "unsafe")

_TASK_RE = re.compile(r"PatchPilot-Task:\s*(?P<task>[A-Z_]+)\s*\(attempt (?P<attempt>\d+)\)")
_CONTEXT_FILE_RE = re.compile(r"^### (?P<path>[^\s:]+) :: (?P<symbol>\S+)", re.MULTILINE)


@dataclass(slots=True)
class Edit:
    path: str
    find: str
    replace: str


@dataclass(slots=True)
class Solution:
    name: str
    summary: str
    plan: dict[str, Any]
    edits: list[Edit] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def matches(self, workspace: Path) -> bool:
        """A solution applies when every one of its anchors is present verbatim."""
        for edit in self.edits:
            target = workspace / edit.path
            if not target.is_file():
                return False
            try:
                content = target.read_text(encoding="utf-8")
            except OSError:
                return False
            if edit.find not in content:
                return False
        return True


def load_solutions(directory: Path) -> list[Solution]:
    """Load every scripted solution from a directory of YAML files."""
    solutions: list[Solution] = []
    if not directory.is_dir():
        return solutions
    for file in sorted(directory.glob("*.y*ml")):
        try:
            payload = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            logger.warning(
                "skipping unreadable solution file",
                extra={"file": str(file), "error": str(exc)},
            )
            continue
        for entry in payload.get("solutions", []):
            solutions.append(
                Solution(
                    name=entry.get("name", file.stem),
                    summary=entry.get("summary", ""),
                    plan=entry.get("plan", {}),
                    tags=list(entry.get("tags", [])),
                    edits=[
                        Edit(
                            path=item["path"],
                            find=item["find"],
                            replace=item["replace"],
                        )
                        for item in entry.get("edits", [])
                    ],
                )
            )
    return solutions


def default_solutions_dir() -> Path:
    import os

    override = os.environ.get("PATCHPILOT_MOCK_SOLUTIONS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[4] / "fixtures" / "solutions"


class MockAdapter:
    """Implements ``LLMAdapter`` and ``WorkspaceAware`` with scripted behaviour."""

    def __init__(
        self,
        variant: str = "deterministic",
        *,
        solutions_dir: Path | None = None,
        latency_ms: int = 5,
    ) -> None:
        if variant not in MOCK_VARIANTS:
            raise ValueError(
                f"unknown mock variant {variant!r}; expected one of {', '.join(MOCK_VARIANTS)}"
            )
        self.name = "mock"
        self.variant = variant
        self.model = f"mock:{variant}"
        self.latency_ms = latency_ms
        self.solutions_dir = solutions_dir or default_solutions_dir()
        self._solutions: list[Solution] | None = None
        self.workspace: Path | None = None
        self.repo_sha: str = ""
        self.calls: list[tuple[str, int]] = []

    # ------------------------------------------------------------- workspace
    def bind_workspace(self, path: Path, repo_sha: str) -> None:
        self.workspace = Path(path)
        self.repo_sha = repo_sha

    @property
    def solutions(self) -> list[Solution]:
        if self._solutions is None:
            self._solutions = load_solutions(self.solutions_dir)
        return self._solutions

    def find_solution(self) -> Solution | None:
        if self.workspace is None:
            return None
        for solution in self.solutions:
            if solution.matches(self.workspace):
                return solution
        return None

    # --------------------------------------------------------------- protocol
    def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        prompt = "\n".join(message.content for message in messages)
        task, attempt = self._task_of(prompt)
        self.calls.append((task, attempt))

        text = self._plan(prompt, attempt) if task == "PLAN" else self._patch(prompt, attempt)

        elapsed = max(int((time.perf_counter() - started) * 1000), self.latency_ms)
        return LLMResponse(
            text=text,
            model=self.model,
            usage=TokenUsage(
                input_tokens=sum(estimate_tokens(m.content) for m in messages),
                output_tokens=estimate_tokens(text),
            ),
            latency_ms=elapsed,
            finish_reason="stop",
            raw={"provider": "mock", "variant": self.variant, "usage_estimated": True},
        )

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _task_of(prompt: str) -> tuple[str, int]:
        match = _TASK_RE.search(prompt)
        if match is None:
            return ("PLAN" if "repair plan" in prompt.lower() else "GENERATE_PATCH", 1)
        return match.group("task"), int(match.group("attempt"))

    @staticmethod
    def _context_paths(prompt: str) -> list[str]:
        seen: list[str] = []
        for match in _CONTEXT_FILE_RE.finditer(prompt):
            path = match.group("path")
            if path not in seen:
                seen.append(path)
        return seen

    def _plan(self, prompt: str, attempt: int) -> str:
        solution = self.find_solution()
        context_paths = self._context_paths(prompt)

        if self.variant == "broken":
            return json.dumps(
                {"thoughts": "I am not going to follow the schema.", "confidence": "high"}
            )

        if solution is not None and self.variant in ("deterministic", "flaky", "stubborn"):
            plan = dict(solution.plan)
            plan.setdefault("files_to_change", [edit.path for edit in solution.edits])
            plan.setdefault("confidence", 0.8)
            if self.variant == "stubborn":
                plan["root_cause"] = (
                    "The reporting layer is probably formatting the value incorrectly "
                    "before it is returned to the caller."
                )
                plan["patch_strategy"] = (
                    "Adjust the surrounding code and re-run the tests to see whether "
                    "the failure moves."
                )
                plan["confidence"] = 0.35
            return json.dumps(plan, indent=2)

        return json.dumps(
            {
                "root_cause": (
                    "No scripted solution matched this repository, so the mock adapter "
                    "cannot diagnose the defect."
                ),
                "files_to_change": context_paths[:2],
                "tests_to_run": [],
                "patch_strategy": (
                    "The deterministic mock has no edit for this repository; a real "
                    "model is required."
                ),
                "assumptions": ["The mock adapter is a test double, not a model."],
                "uncertainties": ["Everything: no reasoning was performed."],
                "confidence": 0.0,
            },
            indent=2,
        )

    def _patch(self, prompt: str, attempt: int) -> str:
        if self.variant == "broken":
            return (
                "I looked at the code and I think the problem is somewhere in the "
                "helper function. You should probably guard the denominator."
            )

        if self.variant == "unsafe":
            return self._ci_workflow_patch()

        if self.variant == "flaky" and attempt == 1:
            return self._malformed_patch(prompt)

        if self.variant == "stubborn":
            return self._decoy_patch(prompt, attempt)

        solution = self.find_solution()
        if solution is None:
            return "INSUFFICIENT_CONTEXT"
        return self._solution_patch(solution)

    def _solution_patch(self, solution: Solution) -> str:
        """Render the scripted edits as a diff against the live workspace.

        Edits are grouped per file and applied in sequence to the same buffer, so
        several edits to one file produce one multi-hunk file section -- what a
        real model would emit, and what a reviewer expects to read.
        """
        assert self.workspace is not None
        grouped: dict[str, list[Edit]] = {}
        for edit in solution.edits:
            grouped.setdefault(edit.path, []).append(edit)

        diffs: list[str] = []
        for path, edits in grouped.items():
            target = self.workspace / path
            before = target.read_text(encoding="utf-8")
            after = before
            for edit in edits:
                if edit.find not in after:
                    continue
                after = after.replace(edit.find, edit.replace, 1)
            if after == before:
                continue
            diff = make_unified_diff(path, before, after)
            if diff:
                diffs.append(diff)
        return "".join(diffs) or "INSUFFICIENT_CONTEXT"

    def _decoy_patch(self, prompt: str, attempt: int) -> str:
        """A patch that applies cleanly and changes nothing that matters."""
        if self.workspace is None:
            return "INSUFFICIENT_CONTEXT"
        for path in self._context_paths(prompt):
            target = self.workspace / path
            if not target.is_file() or path.endswith((".md", ".toml", ".yaml", ".yml")):
                continue
            before = target.read_text(encoding="utf-8")
            lines = before.splitlines(keepends=True)
            if len(lines) < 3:
                continue
            marker = f"# NOTE: reviewed during repair attempt {attempt}\n"
            if marker in before:
                continue
            after = "".join(lines[:1]) + marker + "".join(lines[1:])
            diff = make_unified_diff(path, before, after)
            if diff:
                return diff
        return "INSUFFICIENT_CONTEXT"

    def _malformed_patch(self, prompt: str) -> str:
        path = next(iter(self._context_paths(prompt)), "unknown.py")
        return f"--- a/{path}\n+++ b/{path}\n@@ this is not a hunk header @@\n+  # attempted fix\n"

    @staticmethod
    def _ci_workflow_patch() -> str:
        return (
            "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
            "--- a/.github/workflows/ci.yml\n"
            "+++ b/.github/workflows/ci.yml\n"
            "@@ -1,3 +1,3 @@\n"
            " name: ci\n"
            "-on: [push]\n"
            "+on: []\n"
            " jobs:\n"
        )
