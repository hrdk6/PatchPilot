"""Shared test fixtures.

Two rules hold throughout the suite:

* **No network and no API keys.** Every test uses the deterministic mock
  adapters, the local hash embedder and the in-process vector store.
* **No shared state on disk.** Each test gets its own data directory, database
  and vector store via ``tmp_path``, so tests can run in any order.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from patchpilot_core.config import Settings, reset_settings
from patchpilot_core.logging import configure_logging
from patchpilot_core.models import IssueSpec, RepositorySpec, RunConfig, SandboxLimits

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_REPOS = REPO_ROOT / "fixtures" / "repos"
SOLUTIONS_DIR = REPO_ROOT / "fixtures" / "solutions"
DATASETS_DIR = REPO_ROOT / "fixtures" / "datasets"
REFERENCE_PATCHES = REPO_ROOT / "fixtures" / "reference_patches"


@pytest.fixture(autouse=True)
def _quiet_logging() -> None:
    configure_logging("CRITICAL", json_output=False)


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Never touch the developer's real ``.patchpilot`` directory or database."""
    monkeypatch.setenv("PATCHPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "PATCHPILOT_DATABASE_URL", f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}"
    )
    monkeypatch.setenv("PATCHPILOT_SANDBOX_BACKEND", "local")
    monkeypatch.setenv("PATCHPILOT_LOG_LEVEL", "CRITICAL")
    monkeypatch.delenv("PATCHPILOT_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PATCHPILOT_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PATCHPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PATCHPILOT_QDRANT_URL", raising=False)
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    created = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}",
        sandbox_backend="local",
        log_level="CRITICAL",
    )
    created.ensure_dirs()
    return created


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A writable copy of the calc_service fixture."""
    destination = tmp_path / "calc_service"
    shutil.copytree(FIXTURE_REPOS / "calc_service", destination)
    return destination


@pytest.fixture
def pipeline_repo(tmp_path: Path) -> Path:
    """A writable copy of the text_pipeline fixture (bug one import hop away)."""
    destination = tmp_path / "text_pipeline"
    shutil.copytree(FIXTURE_REPOS / "text_pipeline", destination)
    return destination


@pytest.fixture
def sample_issue() -> IssueSpec:
    return IssueSpec(
        source="manual",
        title="percentage() raises ZeroDivisionError for an empty bucket",
        body=(
            "Calling percentage(0, 0) raises ZeroDivisionError. An empty bucket "
            "expected nothing, so it should report 0.0%."
        ),
    )


@pytest.fixture
def run_config() -> RunConfig:
    return RunConfig(
        model="mock:deterministic",
        max_repair_attempts=3,
        sandbox_backend="local",
        limits=SandboxLimits(timeout_seconds=120, memory_mb=512),
    )


@pytest.fixture
def repo_spec(fixture_repo: Path) -> RepositorySpec:
    return RepositorySpec(url=str(fixture_repo), name="calc_service")


def docker_available() -> bool:
    from patchpilot_sandbox import DockerSandbox

    available, _ = DockerSandbox().available()
    return available


requires_docker = pytest.mark.skipif(not docker_available(), reason="no reachable Docker daemon")
