"""Checkouts are immutable, shared safely between concurrent runs, and symlink-free."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest
from patchpilot_agent import ingest as ingest_module
from patchpilot_agent import ingest_repository
from patchpilot_core.config import Settings
from patchpilot_core.errors import RepositoryError
from patchpilot_core.models import RepositorySpec

pytestmark = pytest.mark.skipif(os.name != "posix", reason="creates POSIX symlinks")


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=PatchPilot Tests",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    directory = tmp_path / "host-secrets"
    directory.mkdir()
    (directory / "id_rsa").write_text("-----BEGIN PRIVATE KEY----- TOP-SECRET\n")
    return directory


@pytest.fixture
def hostile_repo(fixture_repo: Path, secret: Path) -> Path:
    """The calc_service fixture plus links pointing out of the repository."""
    (fixture_repo / "leak.py").symlink_to(secret / "id_rsa")
    (fixture_repo / "everything").symlink_to(secret)
    return fixture_repo


@pytest.fixture
def remote(tmp_path: Path, fixture_repo: Path) -> Path:
    """A git repository standing in for a remote, cloned via ``_ingest_git``."""
    git(fixture_repo, "init", "-q")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-q", "-m", "fixture")
    return fixture_repo


def clone(url: Path, settings: Settings, **spec: str):
    # A plain path is a valid `git clone` source, which exercises the clone path
    # (ingest_repository would route a local path to the copy path instead).
    return ingest_module._ingest_git(RepositorySpec(url=str(url), **spec), settings, None)


def contains_secret(root: Path) -> bool:
    for path in root.rglob("*"):
        if (
            path.is_file()
            and not path.is_symlink()
            and "TOP-SECRET" in path.read_text(errors="replace")
        ):
            return True
    return False


class TestSymlinks:
    def test_a_local_copy_does_not_follow_links_out_of_the_repository(
        self, settings: Settings, hostile_repo: Path
    ) -> None:
        """Copying used to dereference links, turning a key into a source file."""
        ingested = ingest_repository(RepositorySpec(url=str(hostile_repo)), settings)
        assert not (ingested.path / "leak.py").exists()
        assert not (ingested.path / "everything").exists()
        assert not contains_secret(ingested.path)
        assert (ingested.path / "calc_service" / "operations.py").is_file()

    def test_a_clone_carries_no_symlinks(
        self, settings: Settings, remote: Path, secret: Path
    ) -> None:
        (remote / "leak.py").symlink_to(secret / "id_rsa")
        git(remote, "add", "-A")
        git(remote, "commit", "-q", "-m", "add a link")

        ingested = clone(remote, settings)
        assert not any(path.is_symlink() for path in ingested.path.rglob("*"))
        assert not contains_secret(ingested.path)


class TestImmutableCheckouts:
    def test_reingesting_never_replaces_a_checkout_in_use(
        self, settings: Settings, remote: Path
    ) -> None:
        """Every git ingest used to delete the cached checkout and clone again."""
        first = clone(remote, settings)
        # Stands in for "a run is reading this tree": it must still be there,
        # untouched, after another ingest of the same repository.
        in_use = first.path / "in-use.marker"
        in_use.write_text("run A is using this checkout")

        second = clone(remote, settings)
        assert second.path == first.path
        assert in_use.read_text() == "run A is using this checkout"

    def test_concurrent_ingests_share_one_complete_checkout(
        self, settings: Settings, remote: Path
    ) -> None:
        results: list[Path] = []
        errors: list[BaseException] = []

        def work() -> None:
            try:
                results.append(clone(remote, settings).path)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        assert len(set(results)) == 1
        assert (results[0] / "calc_service" / "operations.py").is_file()
        assert not list(settings.repo_cache_root.glob(".staging-*"))

    def test_new_content_gets_a_new_checkout_and_the_old_one_survives(
        self, settings: Settings, fixture_repo: Path
    ) -> None:
        spec = RepositorySpec(url=str(fixture_repo))
        before = ingest_repository(spec, settings)
        (fixture_repo / "calc_service" / "extra.py").write_text("VALUE = 1\n")
        after = ingest_repository(spec, settings)

        assert after.path != before.path
        assert after.repo_sha != before.repo_sha
        assert (before.path / "calc_service" / "operations.py").is_file()
        assert not (before.path / "calc_service" / "extra.py").exists()

    def test_a_pinned_sha_is_served_from_the_cache_without_git(
        self, settings: Settings, remote: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sha = git(remote, "rev-parse", "HEAD")
        first = clone(remote, settings, commit_sha=sha)

        def no_git(*_args, **_kwargs):
            raise AssertionError("git should not run for a cached, pinned checkout")

        monkeypatch.setattr(ingest_module, "_run_git", no_git)
        second = clone(remote, settings, commit_sha=sha.upper())
        assert second.path == first.path
        assert second.repo_sha == sha

    def test_a_failed_clone_leaves_no_staging_directory(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        with pytest.raises(RepositoryError):
            clone(tmp_path / "does-not-exist", settings)
        assert not list(settings.repo_cache_root.glob(".staging-*"))
