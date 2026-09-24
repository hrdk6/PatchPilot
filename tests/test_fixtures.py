"""Consistency checks on the bundled fixtures.

These guard against the quiet kind of rot that makes a benchmark meaningless: a
fixture that no longer fails, a scripted solution whose anchor has drifted, a
reference patch that no longer matches the fix, or an answer key that leaked into
the repository under test.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FIXTURE_REPOS, REFERENCE_PATCHES, SOLUTIONS_DIR
from patchpilot_agent.adapters.mock import MockAdapter, load_solutions
from patchpilot_core.diffutil import apply_patch_to_tree, parse_unified_diff
from patchpilot_evals import load_dataset

FIXTURE_NAMES = ["calc_service", "text_pipeline", "task_queue"]


def run_suite(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.fixture(params=FIXTURE_NAMES)
def fixture_copy(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[str, Path]:
    name = request.param
    destination = tmp_path / name
    shutil.copytree(FIXTURE_REPOS / name, destination)
    return name, destination


class TestFixtureRepositories:
    def test_every_fixture_fails_before_patching(self, fixture_copy: tuple[str, Path]) -> None:
        name, repo = fixture_copy
        result = run_suite(repo)
        assert result.returncode != 0, f"{name} passes on a clean checkout, so it tests nothing"
        assert "failed" in result.stdout.lower()

    def test_the_scripted_solution_makes_every_fixture_pass(
        self, fixture_copy: tuple[str, Path]
    ) -> None:
        name, repo = fixture_copy
        adapter = MockAdapter("deterministic")
        adapter.bind_workspace(repo, "sha")
        solution = adapter.find_solution()
        assert solution is not None, f"no scripted solution matched {name}"

        diff = adapter._solution_patch(solution)
        apply_patch_to_tree(repo, diff)

        result = run_suite(repo)
        assert result.returncode == 0, (
            f"{name} still fails after the scripted fix:\n{result.stdout}\n{result.stderr}"
        )

    def test_no_fixture_contains_its_own_answer_key(self) -> None:
        """A model must not be able to read the fix out of the repository."""
        for name in FIXTURE_NAMES:
            for path in (FIXTURE_REPOS / name).rglob("*"):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                relative = path.relative_to(FIXTURE_REPOS).as_posix()
                assert "solution" not in path.name.lower(), relative
                assert not path.name.endswith(".diff"), relative

    def test_fixture_readmes_do_not_give_away_the_defect(self) -> None:
        for name in FIXTURE_NAMES:
            readme = (FIXTURE_REPOS / name / "README.md").read_text(encoding="utf-8").lower()
            for giveaway in ("the bug", "the defect", "off-by-one", "zerodivision"):
                assert giveaway not in readme, f"{name}/README.md leaks the answer: {giveaway}"


class TestScriptedSolutions:
    def test_every_anchor_still_matches_its_fixture(self) -> None:
        solutions = load_solutions(SOLUTIONS_DIR)
        assert len(solutions) == len(FIXTURE_NAMES)
        for solution in solutions:
            for edit in solution.edits:
                matches = [
                    name
                    for name in FIXTURE_NAMES
                    if (FIXTURE_REPOS / name / edit.path).is_file()
                    and edit.find in (FIXTURE_REPOS / name / edit.path).read_text(encoding="utf-8")
                ]
                assert matches, (
                    f"{solution.name}: the anchor for {edit.path} no longer appears in any "
                    "fixture. Regenerate it with infra/scripts/generate_reference_patches.py."
                )

    def test_each_solution_matches_exactly_one_fixture(self) -> None:
        for name in FIXTURE_NAMES:
            adapter = MockAdapter("deterministic")
            adapter.bind_workspace(FIXTURE_REPOS / name, "sha")
            matched = [
                solution for solution in adapter.solutions if solution.matches(FIXTURE_REPOS / name)
            ]
            assert len(matched) == 1, f"{name} matched {len(matched)} solutions"

    def test_every_solution_carries_a_usable_plan(self) -> None:
        from patchpilot_agent import coerce_plan

        for solution in load_solutions(SOLUTIONS_DIR):
            plan = coerce_plan(solution.plan)
            assert plan.root_cause and plan.patch_strategy
            assert plan.files_to_change


class TestReferencePatches:
    def test_reference_patches_are_current(self) -> None:
        """Regenerate with `python infra/scripts/generate_reference_patches.py`."""
        dataset = load_dataset("patchpilot-fixtures")
        mapping = {
            "calc-service-zero-division": "calc_service",
            "text-pipeline-punctuation": "text_pipeline",
            "task-queue-pagination": "task_queue",
        }
        for task in dataset.tasks:
            adapter = MockAdapter("deterministic")
            adapter.bind_workspace(FIXTURE_REPOS / mapping[task.id], "reference")
            solution = adapter.find_solution()
            assert solution is not None
            expected = adapter._solution_patch(solution)
            stored = (REFERENCE_PATCHES / f"{task.id}.diff").read_text(encoding="utf-8")
            assert stored == expected, f"{task.id}.diff is stale; regenerate the reference patches."

    def test_reference_patches_parse_and_touch_the_expected_files(self) -> None:
        dataset = load_dataset("patchpilot-fixtures")
        for task in dataset.tasks:
            diff = (REFERENCE_PATCHES / f"{task.id}.diff").read_text(encoding="utf-8")
            patches = parse_unified_diff(diff)
            touched = {patch.target_path for patch in patches}
            assert set(task.expected_files) <= touched


class TestDatasetIntegrity:
    def test_every_task_points_at_a_real_fixture(self) -> None:
        dataset = load_dataset("patchpilot-fixtures")
        for task in dataset.tasks:
            spec = task.repository_spec(dataset.base_dir)
            assert Path(spec.url).is_dir(), f"{task.id}: {spec.url} does not exist"

    def test_issue_text_describes_symptoms_not_the_fix(self) -> None:
        """An issue that names the fix would test reading comprehension, not repair."""
        dataset = load_dataset("patchpilot-fixtures")
        for task in dataset.tasks:
            body = task.issue.lower()
            for expected_file in task.expected_files:
                # Naming the file to change is too strong a hint for these tasks.
                assert expected_file.lower() not in body, (
                    f"{task.id} names {expected_file} in the issue body"
                )
