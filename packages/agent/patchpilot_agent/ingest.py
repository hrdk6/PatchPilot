"""Repository and issue ingestion.

Two jobs, both of which must be reproducible:

* **Repository** -- clone (or copy) a checkout and pin it to an immutable
  identifier. For a git remote that is the resolved commit SHA. For a local
  directory that is not a git repository -- the fixtures in this repo, for
  example -- it is a content digest (``sha256:...``), which pins a run to exact
  file contents just as firmly.
* **Issue** -- fetch from the GitHub API when a token is present, otherwise use
  text the user pasted. A GitHub token is an enhancement, never a requirement:
  every code path here works without one.

Nothing in this module executes repository code. Clones are non-recursive (no
submodule payloads) and run with credential prompting disabled.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from patchpilot_core.config import Settings, get_settings
from patchpilot_core.errors import RepositoryError
from patchpilot_core.logging import get_logger
from patchpilot_core.models import IssueSpec, RepositorySpec

logger = get_logger(__name__, component="ingest")

GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
}

CLONE_TIMEOUT = 300
GITHUB_URL_RE = re.compile(r"github\.com[:/](?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$")

COPY_EXCLUDES = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache"}


@dataclass(slots=True)
class IngestedRepository:
    spec: RepositorySpec
    path: Path
    repo_sha: str
    source: str
    """``git-clone``, ``git-worktree`` or ``local-copy``."""

    default_branch: str | None = None


def is_local_source(url: str) -> bool:
    return url.startswith("file://") or not url.startswith(("http://", "https://", "git@"))


def local_path_for(url: str) -> Path:
    if url.startswith("file://"):
        return Path(url.removeprefix("file://"))
    return Path(url)


def content_digest(root: Path) -> str:
    """Stable digest of the tree contents, used to pin non-git sources."""
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_dir() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in COPY_EXCLUDES for part in relative.split("/")):
            continue
        hasher.update(relative.encode("utf-8"))
        try:
            hasher.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            continue
    return f"sha256:{hasher.hexdigest()[:16]}"


def _run_git(args: list[str], cwd: Path | None = None, timeout: int = CLONE_TIMEOUT):
    env = {**os.environ, **GIT_ENV}
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RepositoryError(
            "git is not installed or not on PATH",
            remediation="Install git, or ingest a local directory instead of a remote URL.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RepositoryError(
            f"git command timed out after {timeout}s: git {' '.join(args)}"
        ) from exc


def ingest_repository(
    spec: RepositorySpec,
    settings: Settings | None = None,
    *,
    destination: Path | None = None,
) -> IngestedRepository:
    """Materialise a pinned checkout on disk."""
    settings = settings or get_settings()
    settings.ensure_dirs()

    if is_local_source(spec.url):
        return _ingest_local(spec, settings, destination)
    return _ingest_git(spec, settings, destination)


def _target_dir(settings: Settings, spec: RepositorySpec, destination: Path | None) -> Path:
    """Where a checkout is cached. Always a direct child of the cache root.

    The name is the sanitised slug plus a hash of the URL and ref. The result is
    then asserted to sit inside the cache root: a name that behaved as an
    absolute path would otherwise make ``/`` discard the root and write the
    checkout somewhere unexpected -- next to the source repository, for example.
    """
    if destination is not None:
        return Path(destination)
    key = hashlib.sha1(
        f"{spec.url}@{spec.commit_sha or spec.branch or 'default'}".encode()
    ).hexdigest()[:12]
    root = settings.repo_cache_root
    target = root / f"{spec.slug}-{key}"
    if target.parent != root:
        raise RepositoryError(
            f"refusing to cache {spec.url!r} outside the cache root {root}",
            remediation="This is a bug; please report the repository URL that triggered it.",
        )
    return target


def _remove_tree(path: Path) -> None:
    """Remove a directory, working around read-only files and transient locks."""

    def _retry(function, target, _exception):  # type: ignore[no-untyped-def]
        try:
            Path(target).chmod(stat.S_IWRITE)
            function(target)
        except OSError:
            pass

    for _ in range(3):
        shutil.rmtree(path, onexc=_retry)
        if not path.exists():
            return
        time.sleep(0.1)
    raise RepositoryError(
        f"could not clear the cached checkout at {path}",
        remediation="Close anything holding those files open, or delete the directory.",
    )


def _ingest_local(
    spec: RepositorySpec, settings: Settings, destination: Path | None
) -> IngestedRepository:
    source = local_path_for(spec.url).resolve()
    if not source.is_dir():
        raise RepositoryError(
            f"local repository path does not exist: {source}",
            remediation="Check the path; it must point at a directory on this machine.",
        )

    target = _target_dir(settings, spec, destination)
    source_digest = content_digest(source)
    if target.is_dir() and content_digest(target) == source_digest:
        # The cached checkout is byte-identical, so re-copying would only risk a
        # Windows file lock for no benefit.
        logger.info("reusing cached checkout", extra={"path": str(target)})
    else:
        if target.exists():
            _remove_tree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source,
            target,
            ignore=lambda _directory, entries: {e for e in entries if e in COPY_EXCLUDES},
            symlinks=False,
        )

    git_dir = source / ".git"
    if git_dir.exists():
        head = _run_git(["rev-parse", "HEAD"], cwd=source)
        if head.returncode == 0:
            sha = head.stdout.strip()
            status = _run_git(["status", "--porcelain"], cwd=source)
            if status.returncode == 0 and status.stdout.strip():
                # The copy includes uncommitted edits, so HEAD alone would name a
                # tree the run never saw -- and let an index cached for one set of
                # edits be reused for another. The content digest makes the
                # identifier change whenever the working tree does.
                sha = f"{sha}+dirty.{source_digest.removeprefix('sha256:')}"
            logger.info("ingested local git checkout", extra={"repo_sha": sha})
            return IngestedRepository(
                spec=spec.model_copy(update={"commit_sha": sha}),
                path=target,
                repo_sha=sha,
                source="local-copy",
            )

    digest = source_digest
    logger.info(
        "ingested local directory",
        extra={"repo_sha": digest, "path": str(target), "source": str(source)},
    )
    return IngestedRepository(
        spec=spec.model_copy(update={"commit_sha": digest}),
        path=target,
        repo_sha=digest,
        source="local-copy",
    )


def _ingest_git(
    spec: RepositorySpec, settings: Settings, destination: Path | None
) -> IngestedRepository:
    target = _target_dir(settings, spec, destination)
    if target.exists():
        _remove_tree(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    clone_args = ["clone", "--no-recurse-submodules", "--quiet"]
    if spec.commit_sha:
        # A specific commit may not be reachable from a shallow clone of a branch.
        clone_args += [spec.url, str(target)]
    else:
        clone_args += ["--depth", "1"]
        if spec.branch:
            clone_args += ["--branch", spec.branch]
        clone_args += [spec.url, str(target)]

    cloned = _run_git(clone_args)
    if cloned.returncode != 0:
        detail = (cloned.stderr or cloned.stdout).strip()[:400]
        raise RepositoryError(
            f"could not clone {spec.url}: {detail}",
            remediation=(
                "Check the URL and that the repository is public. Private "
                "repositories need credentials configured for git itself; "
                "PatchPilot never stores them."
            ),
            context={"url": spec.url, "branch": spec.branch},
        )

    if spec.commit_sha:
        checkout = _run_git(["checkout", "--quiet", spec.commit_sha], cwd=target)
        if checkout.returncode != 0:
            raise RepositoryError(
                f"commit {spec.commit_sha} not found in {spec.url}",
                remediation="Check the SHA, or omit it to use the branch head.",
            )

    head = _run_git(["rev-parse", "HEAD"], cwd=target)
    sha = head.stdout.strip() if head.returncode == 0 else content_digest(target)
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=target)
    logger.info(
        "cloned repository",
        extra={"url": spec.url, "repo_sha": sha, "path": str(target)},
    )
    return IngestedRepository(
        spec=spec.model_copy(update={"commit_sha": sha}),
        path=target,
        repo_sha=sha,
        source="git-clone",
        default_branch=branch.stdout.strip() if branch.returncode == 0 else None,
    )


# --------------------------------------------------------------------------- #
# Issues
# --------------------------------------------------------------------------- #
def parse_github_repo(url: str) -> tuple[str, str] | None:
    match = GITHUB_URL_RE.search(url.strip())
    if match is None:
        return None
    return match.group("owner"), match.group("repo")


def fetch_github_issue(
    repository_url: str, number: int, settings: Settings | None = None
) -> IssueSpec:
    """Fetch an issue from the GitHub API.

    A token raises the rate limit and allows private repositories, but the
    unauthenticated path works for public issues. Failure here is never fatal to
    a run: the caller falls back to pasted issue text.
    """
    settings = settings or get_settings()
    parsed = parse_github_repo(repository_url)
    if parsed is None:
        raise RepositoryError(
            f"{repository_url} is not a GitHub repository URL",
            remediation="Paste the issue text instead of giving an issue number.",
        )
    owner, repo = parsed
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"

    url = f"{settings.github_api_url}/repos/{owner}/{repo}/issues/{number}"
    try:
        response = httpx.get(url, headers=headers, timeout=30.0)
    except httpx.HTTPError as exc:
        raise RepositoryError(
            f"could not reach the GitHub API: {exc}",
            remediation="Paste the issue text to run without network access.",
        ) from exc

    if response.status_code == 404:
        raise RepositoryError(
            f"issue #{number} was not found in {owner}/{repo}",
            remediation="Check the issue number, or paste the issue text instead.",
        )
    if response.status_code == 403 and "rate limit" in response.text.lower():
        raise RepositoryError(
            "the GitHub API rate limit was reached",
            remediation=(
                "Set PATCHPILOT_GITHUB_TOKEN to raise the limit, or paste the issue text."
            ),
        )
    if response.status_code >= 400:
        raise RepositoryError(
            f"GitHub returned HTTP {response.status_code} for issue #{number}",
            context={"body": response.text[:200]},
        )

    payload = response.json()
    return IssueSpec(
        source="github",
        number=payload.get("number", number),
        title=payload.get("title") or "",
        body=payload.get("body") or "",
        url=payload.get("html_url"),
        labels=[label.get("name", "") for label in payload.get("labels", [])],
    )


def resolve_issue(
    repository_url: str,
    *,
    issue_number: int | None = None,
    issue_text: str | None = None,
    issue_title: str | None = None,
    settings: Settings | None = None,
) -> IssueSpec:
    """Prefer pasted text; fall back to the GitHub API; never require a token."""
    if issue_text and issue_text.strip():
        return IssueSpec(
            source="manual",
            number=issue_number,
            title=(issue_title or _first_line(issue_text)).strip(),
            body=issue_text.strip(),
        )
    if issue_number is not None:
        return fetch_github_issue(repository_url, issue_number, settings)
    raise RepositoryError(
        "no issue was provided",
        remediation="Give an issue number for a GitHub repository, or paste the issue text.",
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned[:120]
    return "Untitled issue"
