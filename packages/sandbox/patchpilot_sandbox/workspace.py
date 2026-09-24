"""Disposable workspaces.

Every sandbox attempt gets its own freshly copied tree. The pristine checkout is
never handed to a sandbox and is never patched in place, so a run can always be
re-executed from the same starting state and one attempt can never contaminate
the next.
"""

from __future__ import annotations

import hashlib
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from patchpilot_core.errors import SandboxError
from patchpilot_core.logging import get_logger

logger = get_logger(__name__, component="workspace")

# Never copied into a sandbox: VCS metadata (carries credentials in some setups),
# caches, and local virtualenvs.
COPY_EXCLUDES = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    ".patchpilot",
}

MAX_WORKSPACE_FILES = 20_000
MAX_WORKSPACE_BYTES = 512 * 1024 * 1024


@dataclass(slots=True)
class WorkspaceInfo:
    path: Path
    file_count: int
    total_bytes: int
    digest: str


def _ignore(directory: str, entries: list[str]) -> set[str]:
    skipped = {entry for entry in entries if entry in COPY_EXCLUDES}
    # Symlinks are not copied: a symlink into the host filesystem would defeat the
    # point of copying instead of mounting.
    for entry in entries:
        full = Path(directory) / entry
        try:
            if full.is_symlink():
                skipped.add(entry)
        except OSError:  # pragma: no cover - defensive
            skipped.add(entry)
    return skipped


def copy_snapshot(source: Path, destination: Path) -> WorkspaceInfo:
    """Copy ``source`` into a fresh ``destination`` directory."""
    source = Path(source).resolve()
    destination = Path(destination)
    if not source.is_dir():
        raise SandboxError(f"snapshot source is not a directory: {source}")
    if destination.exists():
        dispose(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    shutil.copytree(source, destination, ignore=_ignore, symlinks=False)
    info = inspect(destination)
    if info.file_count > MAX_WORKSPACE_FILES or info.total_bytes > MAX_WORKSPACE_BYTES:
        dispose(destination)
        raise SandboxError(
            f"repository is too large to sandbox safely "
            f"({info.file_count} files, {info.total_bytes} bytes)",
            remediation="Index and run against a smaller repository or a subdirectory.",
        )
    logger.info(
        "workspace prepared",
        extra={
            "workspace": str(destination),
            "files": info.file_count,
            "bytes": info.total_bytes,
            "digest": info.digest,
        },
    )
    return info


def inspect(root: Path) -> WorkspaceInfo:
    """Count files and compute a content digest for reproducibility checks."""
    hasher = hashlib.sha256()
    count = 0
    total = 0
    for path in sorted(Path(root).rglob("*")):
        if path.is_dir() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        try:
            data = path.read_bytes()
        except OSError:
            continue
        count += 1
        total += len(data)
        hasher.update(relative.encode("utf-8"))
        hasher.update(hashlib.sha256(data).digest())
    return WorkspaceInfo(
        path=Path(root), file_count=count, total_bytes=total, digest=hasher.hexdigest()[:32]
    )


def _force_writable(function, path, exception) -> None:  # type: ignore[no-untyped-def]
    """``shutil.rmtree`` error handler for read-only files (common on Windows)."""
    try:
        Path(path).chmod(stat.S_IWRITE)
        function(path)
    except OSError:  # pragma: no cover - best effort
        logger.warning("could not remove workspace entry", extra={"path": str(path)})


def dispose(path: Path) -> None:
    """Delete a workspace. Never raises: cleanup must not fail a run."""
    target = Path(path)
    if not target.exists():
        return
    shutil.rmtree(target, onexc=_force_writable)


@contextmanager
def disposable_workspace(
    source: Path, destination: Path, *, keep: bool = False
) -> Iterator[WorkspaceInfo]:
    """Copy a snapshot, yield it, and remove it afterwards unless ``keep``."""
    info = copy_snapshot(source, destination)
    try:
        yield info
    finally:
        if not keep:
            dispose(destination)
