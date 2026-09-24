"""Benchmark dataset format and loader.

A dataset is a YAML (or JSON) file describing tasks that are reproducible without
network access. Every task pins a repository *and* a commit -- or, for the
fixture repositories in this project, relies on the content digest that ingestion
computes, which pins file contents just as precisely.

```yaml
name: patchpilot-fixtures
description: Bugs in the fixture repositories shipped with PatchPilot.
tasks:
  - id: calc-service-zero-division
    title: percentage() raises ZeroDivisionError on an empty bucket
    repository_url: fixtures/repos/calc_service
    commit_sha: null            # fixtures pin by content digest
    issue: |
      Calling percentage(0, 0) raises ZeroDivisionError...
    setup_command: null         # the sandbox has no network by default
    baseline_command: python -m pytest -q
    validation_command: python -m pytest -q
    expected_files: [calc_service/operations.py]
    tags: [python, guard-clause]
    difficulty: easy
```
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from patchpilot_core.errors import ValidationError
from patchpilot_core.models import IssueSpec, RepositorySpec

DEFAULT_DATASET_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "datasets"
DIFFICULTIES = ("easy", "medium", "hard")


@dataclass(slots=True)
class BenchmarkTask:
    id: str
    repository_url: str
    issue: str
    validation_command: str
    title: str = ""
    commit_sha: str | None = None
    branch: str | None = None
    setup_command: str | None = None
    baseline_command: str | None = None
    lint_command: str | None = None
    typecheck_command: str | None = None
    expected_files: list[str] = field(default_factory=list)
    reference_patch: str | None = None
    tags: list[str] = field(default_factory=list)
    difficulty: str = "easy"
    max_repair_attempts: int | None = None
    timeout_seconds: int | None = None

    def repository_spec(self, base_dir: Path) -> RepositorySpec:
        url = self.repository_url
        if "://" not in url and not url.startswith("git@"):
            candidate = (base_dir / url).resolve()
            if candidate.is_dir():
                url = str(candidate)
        return RepositorySpec(url=url, branch=self.branch, commit_sha=self.commit_sha)

    def issue_spec(self) -> IssueSpec:
        return IssueSpec(
            source="manual",
            title=self.title or self.id,
            body=self.issue.strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "repository_url": self.repository_url,
            "commit_sha": self.commit_sha,
            "difficulty": self.difficulty,
            "tags": list(self.tags),
            "validation_command": self.validation_command,
            "baseline_command": self.baseline_command,
            "expected_files": list(self.expected_files),
        }


@dataclass(slots=True)
class Dataset:
    name: str
    path: Path
    description: str = ""
    tasks: list[BenchmarkTask] = field(default_factory=list)

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def all_tags(self) -> list[str]:
        tags: set[str] = set()
        for task in self.tasks:
            tags.update(task.tags)
        return sorted(tags)

    def filter_by_tags(self, tags: list[str]) -> Dataset:
        if not tags:
            return self
        wanted = set(tags)
        return Dataset(
            name=self.name,
            path=self.path,
            description=self.description,
            tasks=[task for task in self.tasks if wanted & set(task.tags)],
        )

    def get(self, task_id: str) -> BenchmarkTask:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise ValidationError(f"task {task_id!r} is not in dataset {self.name!r}")


def _coerce_task(payload: dict[str, Any], index: int) -> BenchmarkTask:
    required = ("id", "repository_url", "issue", "validation_command")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise ValidationError(
            f"task #{index} is missing required field(s): {', '.join(missing)}",
            remediation="Every task needs id, repository_url, issue and validation_command.",
        )
    difficulty = str(payload.get("difficulty", "easy")).lower()
    if difficulty not in DIFFICULTIES:
        raise ValidationError(
            f"task {payload['id']!r} has difficulty {difficulty!r}; "
            f"expected one of {', '.join(DIFFICULTIES)}"
        )
    known = {f.name for f in BenchmarkTask.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(payload) - known
    if unknown:
        raise ValidationError(
            f"task {payload['id']!r} has unknown field(s): {', '.join(sorted(unknown))}"
        )
    return BenchmarkTask(**{key: value for key, value in payload.items() if key in known})


def load_dataset(path: Path | str) -> Dataset:
    """Load and validate a dataset file. Accepts a path, or a bare dataset name."""
    candidate = Path(path)
    if not candidate.exists():
        for suffix in (".yaml", ".yml", ".json"):
            alternative = DEFAULT_DATASET_DIR / f"{candidate.name}{suffix}"
            if alternative.exists():
                candidate = alternative
                break
    if not candidate.exists():
        raise ValidationError(
            f"dataset {path!r} was not found",
            remediation=f"Datasets live in {DEFAULT_DATASET_DIR}.",
        )

    text = candidate.read_text(encoding="utf-8")
    try:
        payload = json.loads(text) if candidate.suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValidationError(f"dataset {candidate} is not valid: {exc}") from exc

    if not isinstance(payload, dict) or "tasks" not in payload:
        raise ValidationError(f"dataset {candidate} must be a mapping with a 'tasks' list")

    tasks = [_coerce_task(item, index) for index, item in enumerate(payload["tasks"])]
    ids = [task.id for task in tasks]
    duplicates = {identifier for identifier in ids if ids.count(identifier) > 1}
    if duplicates:
        raise ValidationError(f"duplicate task ids in {candidate}: {', '.join(sorted(duplicates))}")

    return Dataset(
        name=payload.get("name", candidate.stem),
        path=candidate.resolve(),
        description=payload.get("description", ""),
        tasks=tasks,
    )


def discover_datasets(directory: Path | None = None) -> list[Dataset]:
    """Every dataset in the datasets directory, skipping unreadable ones."""
    directory = directory or DEFAULT_DATASET_DIR
    found: list[Dataset] = []
    if not directory.is_dir():
        return found
    for file in sorted(directory.iterdir()):
        if file.suffix.lower() not in (".yaml", ".yml", ".json"):
            continue
        try:
            found.append(load_dataset(file))
        except ValidationError:
            continue
    return found
