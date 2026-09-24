"""Patch safety policy and application.

Nothing a model produces is trusted. Before a diff is allowed anywhere near a
sandbox it has to survive every check in :class:`PatchValidator`:

* it parses as a unified diff;
* it is not a binary patch;
* no path escapes the repository (``..``, absolute paths, drive letters);
* no protected path is touched -- CI config, lockfiles, Docker build files,
  git internals, dotfiles, secrets -- unless the run config explicitly allows it;
* the file count and line count stay inside the configured budget;
* it applies cleanly to the pristine checkout;
* it actually changes something, and it is not a repeat of a previous attempt.

A rejection is data, not an exception: the reasons are recorded, shown in the
UI, and fed back to the model on the next attempt.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from patchpilot_core.diffutil import (
    DiffApplyError,
    DiffParseError,
    FilePatch,
    apply_file_patch,
    apply_patch_to_tree,
    extract_diff_block,
    parse_unified_diff,
)
from patchpilot_core.enums import PatchRejectionReason
from patchpilot_core.logging import get_logger
from patchpilot_core.models import FileChange, PatchProposal, PatchValidationResult

logger = get_logger(__name__, component="patcher")

# Paths a repair patch has no business touching. Changing these is how an agent
# turns "fix the bug" into "disable the tests" or "add a dependency".
PROTECTED_GLOBS: tuple[str, ...] = (
    ".github/**",
    ".gitlab-ci.yml",
    ".circleci/**",
    "azure-pipelines.yml",
    "Jenkinsfile",
    ".pre-commit-config.yaml",
    "Dockerfile",
    "Dockerfile.*",
    "*/Dockerfile",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
    ".git/**",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "id_rsa",
    "id_ed25519",
    ".npmrc",
    ".pypirc",
    ".netrc",
)

_ABSOLUTE_RE = re.compile(r"^(/|[A-Za-z]:[\\/])")


@dataclass(slots=True)
class PatchPolicy:
    max_files: int = 10
    max_lines: int = 400
    allow_hidden_files: bool = False
    allowed_globs: tuple[str, ...] = ()
    protected_globs: tuple[str, ...] = PROTECTED_GLOBS

    def is_allowed_override(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.allowed_globs)

    def is_protected(self, path: str) -> bool:
        if self.is_allowed_override(path):
            return False
        return any(_glob_match(path, pattern) for pattern in self.protected_globs)

    def is_hidden(self, path: str) -> bool:
        if self.allow_hidden_files or self.is_allowed_override(path):
            return False
        return any(part.startswith(".") for part in path.split("/"))


def _glob_match(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatch(path, pattern)


class PatchValidator:
    """Applies :class:`PatchPolicy` to a proposed diff."""

    def __init__(self, root: Path, policy: PatchPolicy | None = None) -> None:
        self.root = Path(root)
        self.policy = policy or PatchPolicy()

    def validate(
        self,
        proposal: PatchProposal,
        *,
        previous_hashes: set[str] | None = None,
    ) -> PatchValidationResult:
        result = PatchValidationResult(valid=True)

        diff = proposal.diff.strip()
        if not diff or diff == "INSUFFICIENT_CONTEXT":
            result.reject(
                PatchRejectionReason.EMPTY,
                "The model returned no diff"
                + (" (it reported INSUFFICIENT_CONTEXT)." if diff else "."),
            )
            return result

        try:
            patches = parse_unified_diff(diff)
        except DiffParseError as exc:
            result.reject(
                PatchRejectionReason.MALFORMED,
                f"The diff could not be parsed: {exc}",
            )
            return result

        self._check_paths(patches, result)
        self._check_size(patches, result)
        if not result.valid:
            return result

        self._check_applies(patches, result)
        if not result.valid:
            return result

        if result.lines_added == 0 and result.lines_removed == 0:
            result.reject(PatchRejectionReason.NO_CHANGE, "The diff does not change any line.")
            return result

        if previous_hashes and proposal.normalized_hash() in previous_hashes:
            result.reject(
                PatchRejectionReason.DUPLICATE,
                "This patch is equivalent to one already tried in this run.",
            )
        return result

    # ---------------------------------------------------------------- checks
    def _check_paths(self, patches: list[FilePatch], result: PatchValidationResult) -> None:
        for patch in patches:
            if patch.is_binary:
                result.reject(
                    PatchRejectionReason.BINARY_PATCH,
                    f"Binary patch for {patch.target_path} is not allowed.",
                )
                continue

            for candidate in {patch.old_path, patch.new_path} - {"/dev/null"}:
                normalised = candidate.replace("\\", "/")
                if ".." in normalised.split("/"):
                    result.reject(
                        PatchRejectionReason.PATH_TRAVERSAL,
                        f"Path traversal attempt in the diff: {candidate}",
                    )
                    continue
                if _ABSOLUTE_RE.match(normalised):
                    result.reject(
                        PatchRejectionReason.PATH_TRAVERSAL,
                        f"Absolute path in the diff: {candidate}",
                    )
                    continue
                if self.policy.is_protected(normalised):
                    result.reject(
                        PatchRejectionReason.PROTECTED_PATH,
                        f"{candidate} is a protected path (CI config, lockfile, "
                        "container build file or secret) and may not be patched.",
                    )
                    continue
                if self.policy.is_hidden(normalised):
                    result.reject(
                        PatchRejectionReason.HIDDEN_FILE,
                        f"{candidate} is a hidden file; patching it is disabled by policy.",
                    )
                    continue
                if patch.is_new_file:
                    target = (self.root / normalised).resolve()
                    root = self.root.resolve()
                    if root not in target.parents:
                        result.reject(
                            PatchRejectionReason.NEW_FILE_OUTSIDE_REPO,
                            f"New file {candidate} would be created outside the repository.",
                        )

    def _check_size(self, patches: list[FilePatch], result: PatchValidationResult) -> None:
        changes: list[FileChange] = []
        added = 0
        removed = 0
        for patch in patches:
            change_type: Literal["modify", "add", "delete", "rename"] = "modify"
            if patch.is_new_file:
                change_type = "add"
            elif patch.is_deletion:
                change_type = "delete"
            elif patch.is_rename:
                change_type = "rename"
            changes.append(
                FileChange(
                    path=patch.target_path,
                    change_type=change_type,
                    old_path=patch.old_path if patch.is_rename else None,
                    lines_added=patch.lines_added,
                    lines_removed=patch.lines_removed,
                )
            )
            added += patch.lines_added
            removed += patch.lines_removed

        result.changes = changes
        result.files_changed = len({change.path for change in changes})
        result.lines_added = added
        result.lines_removed = removed

        if result.files_changed > self.policy.max_files:
            result.reject(
                PatchRejectionReason.TOO_MANY_FILES,
                f"The patch touches {result.files_changed} files; the limit is "
                f"{self.policy.max_files}.",
            )
        if added + removed > self.policy.max_lines:
            result.reject(
                PatchRejectionReason.TOO_MANY_LINES,
                f"The patch changes {added + removed} lines; the limit is {self.policy.max_lines}.",
            )

    def _check_applies(self, patches: list[FilePatch], result: PatchValidationResult) -> None:
        """Dry-run the patch in memory against the pristine checkout."""
        buffers: dict[str, str | None] = {}
        for patch in patches:
            target = patch.old_path if not patch.is_new_file else patch.new_path
            if target not in buffers:
                file = self.root / target
                buffers[target] = (
                    file.read_text(encoding="utf-8", errors="replace") if file.is_file() else None
                )
            try:
                updated = apply_file_patch(buffers[target], patch)
            except DiffApplyError as exc:
                result.reject(PatchRejectionReason.APPLY_FAILED, str(exc))
                return
            destination = patch.new_path if not patch.is_deletion else target
            buffers[destination] = updated


def apply_patch(workspace: Path, proposal: PatchProposal) -> list[str]:
    """Apply a validated patch to a workspace. Returns the touched paths."""
    diff = extract_diff_block(proposal.diff) or proposal.diff
    report = apply_patch_to_tree(workspace, diff)
    logger.info(
        "patch applied to workspace",
        extra={
            "workspace": str(workspace),
            "attempt": proposal.attempt,
            "files": report.touched,
        },
    )
    return report.touched
