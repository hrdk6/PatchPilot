#!/usr/bin/env python
"""Regenerate ``fixtures/reference_patches/*.diff`` from the scripted solutions.

The scripted solutions in ``fixtures/solutions`` are the single source of truth
for what "the right fix" is. The reference patches are a rendering of them
against the current fixture sources, used for the benchmark similarity signal.

Run this whenever a fixture repository or a solution anchor changes:

    python infra/scripts/generate_reference_patches.py

``tests/test_fixtures.py::TestReferencePatches::test_reference_patches_are_current``
fails if they drift, so this never silently goes stale.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages" / "core"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "agent"))

from patchpilot_agent.adapters.mock import MockAdapter  # noqa: E402

FIXTURE_REPOS = REPO_ROOT / "fixtures" / "repos"
OUTPUT_DIR = REPO_ROOT / "fixtures" / "reference_patches"

# Dataset task id -> fixture repository directory.
TASKS = {
    "calc-service-zero-division": "calc_service",
    "text-pipeline-punctuation": "text_pipeline",
    "task-queue-pagination": "task_queue",
}


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []

    for task_id, repository in TASKS.items():
        adapter = MockAdapter("deterministic")
        adapter.bind_workspace(FIXTURE_REPOS / repository, "reference")
        solution = adapter.find_solution()
        if solution is None:
            print(f"ERROR: no scripted solution matched {repository}", file=sys.stderr)
            return 1

        diff = adapter._solution_patch(solution)
        if diff == "INSUFFICIENT_CONTEXT":
            print(f"ERROR: solution for {repository} produced no diff", file=sys.stderr)
            return 1

        target = OUTPUT_DIR / f"{task_id}.diff"
        previous = target.read_text(encoding="utf-8") if target.exists() else None
        if previous != diff:
            target.write_text(diff, encoding="utf-8", newline="\n")
            changed.append(target.name)
        print(f"{target.name:<40} {len(diff.splitlines()):>3} lines")

    print(f"\n{len(changed)} file(s) updated" if changed else "\nalready up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
