"""A dependency-free unified-diff parser and applier.

PatchPilot does not shell out to ``git apply`` or ``patch`` to apply model output.
Doing so would mean running a binary over attacker-influenced input on the host
*before* the sandbox exists, and it would make patch application untestable on
machines without those tools. Instead we parse the diff into a typed structure we
can inspect (that same structure powers the safety validator) and apply it in
pure Python against an in-memory snapshot of the file.

Matching is strict on content but tolerant of line-number drift: a hunk whose
context does not match at the stated offset is searched for within
``MAX_OFFSET`` lines. Anything else is a hard failure -- silent fuzzy matching is
how bad patches get applied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAX_OFFSET = 200

_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<heading>.*)$"
)
_DIFF_GIT_RE = re.compile(r"^diff --git (?:a/)?(?P<old>.+?) (?:b/)?(?P<new>.+)$")

DEV_NULL = "/dev/null"


class DiffParseError(ValueError):
    """The text handed to us is not a usable unified diff."""


class DiffApplyError(RuntimeError):
    """The diff parsed, but could not be applied to the given content."""


@dataclass(slots=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, str]] = field(default_factory=list)
    """``(op, text)`` where op is one of ``' '``, ``'-'``, ``'+'``."""

    heading: str = ""
    no_newline_old: bool = False
    no_newline_new: bool = False

    @property
    def added(self) -> int:
        return sum(1 for op, _ in self.lines if op == "+")

    @property
    def removed(self) -> int:
        return sum(1 for op, _ in self.lines if op == "-")

    def old_lines(self) -> list[str]:
        return [text for op, text in self.lines if op in (" ", "-")]

    def new_lines(self) -> list[str]:
        return [text for op, text in self.lines if op in (" ", "+")]


@dataclass(slots=True)
class FilePatch:
    old_path: str
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False
    mode_change: str | None = None

    @property
    def is_new_file(self) -> bool:
        return self.old_path == DEV_NULL

    @property
    def is_deletion(self) -> bool:
        return self.new_path == DEV_NULL

    @property
    def is_rename(self) -> bool:
        return not self.is_new_file and not self.is_deletion and self.old_path != self.new_path

    @property
    def target_path(self) -> str:
        return self.old_path if self.is_deletion else self.new_path

    @property
    def lines_added(self) -> int:
        return sum(h.added for h in self.hunks)

    @property
    def lines_removed(self) -> int:
        return sum(h.removed for h in self.hunks)


def _strip_prefix(raw: str) -> str:
    """Normalise ``a/pkg/mod.py`` / ``b/pkg/mod.py`` to ``pkg/mod.py``."""
    value = raw.strip()
    if value.startswith(('"', "'")) and value.endswith(('"', "'")) and len(value) > 1:
        value = value[1:-1]
    # Drop a trailing timestamp column that some diff tools emit.
    if "\t" in value:
        value = value.split("\t", 1)[0]
    if value == DEV_NULL:
        return DEV_NULL
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return value.replace("\\", "/")


def extract_diff_block(text: str) -> str:
    """Pull the diff out of a model response that may contain prose or fences."""
    if not text:
        return ""
    fenced = re.findall(r"```(?:diff|patch)?\s*\n(.*?)```", text, flags=re.DOTALL)
    for block in fenced:
        if "@@" in block or block.lstrip().startswith(("diff --git", "--- ")):
            return block.strip("\n") + "\n"
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("diff --git ") or (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            return "\n".join(lines[index:]).strip("\n") + "\n"
    return ""


def parse_unified_diff(text: str) -> list[FilePatch]:
    """Parse a unified diff into typed file patches.

    Raises :class:`DiffParseError` when the structure is unusable. Malformed hunk
    counts are tolerated (they are recomputed) but malformed hunk *bodies* are not.
    """
    if not text or not text.strip():
        raise DiffParseError("diff is empty")

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    pending_git_paths: tuple[str, str] | None = None
    index = 0

    def close_hunk() -> None:
        nonlocal hunk
        if hunk is not None and current is not None:
            if not hunk.lines:
                raise DiffParseError(
                    f"hunk at @@ -{hunk.old_start} +{hunk.new_start} @@ has no body"
                )
            hunk.old_count = len(hunk.old_lines())
            hunk.new_count = len(hunk.new_lines())
            current.hunks.append(hunk)
        hunk = None

    while index < len(lines):
        line = lines[index]

        git_match = _DIFF_GIT_RE.match(line)
        if git_match:
            close_hunk()
            current = None
            pending_git_paths = (
                _strip_prefix(git_match.group("old")),
                _strip_prefix(git_match.group("new")),
            )
            index += 1
            continue

        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            close_hunk()
            old, new = pending_git_paths or ("unknown", "unknown")
            patch = FilePatch(old_path=old, new_path=new, is_binary=True)
            patches.append(patch)
            current = None
            pending_git_paths = None
            index += 1
            continue

        if (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            close_hunk()
            old_path = _strip_prefix(line[4:])
            new_path = _strip_prefix(lines[index + 1][4:])
            if old_path == DEV_NULL and new_path == DEV_NULL:
                raise DiffParseError("file patch has /dev/null on both sides")
            if pending_git_paths:
                if old_path == DEV_NULL:
                    new_path = pending_git_paths[1]
                elif new_path == DEV_NULL:
                    old_path = pending_git_paths[0]
            current = FilePatch(old_path=old_path, new_path=new_path)
            patches.append(current)
            pending_git_paths = None
            index += 2
            continue

        hunk_match = _HUNK_RE.match(line)
        if hunk_match:
            if current is None:
                raise DiffParseError("hunk header found before any file header")
            close_hunk()
            hunk = Hunk(
                old_start=int(hunk_match.group("old_start")),
                old_count=int(hunk_match.group("old_count") or 1),
                new_start=int(hunk_match.group("new_start")),
                new_count=int(hunk_match.group("new_count") or 1),
                heading=hunk_match.group("heading").strip(),
            )
            index += 1
            continue

        if hunk is not None:
            if line.startswith("\\ No newline at end of file"):
                if hunk.lines and hunk.lines[-1][0] == "+":
                    hunk.no_newline_new = True
                else:
                    hunk.no_newline_old = True
                index += 1
                continue
            if line[:1] in (" ", "+", "-"):
                hunk.lines.append((line[0], line[1:]))
                index += 1
                continue
            if line == "" and index < len(lines) - 1:
                # A completely empty line inside a hunk is an unprefixed context line
                # (many editors and models strip the trailing space). Only treat it as
                # such while the hunk still expects more lines, and never for the
                # empty string that splitting a newline-terminated diff leaves at the
                # end -- that is punctuation, not content.
                expected_old = hunk.old_count - len(hunk.old_lines())
                expected_new = hunk.new_count - len(hunk.new_lines())
                if expected_old > 0 or expected_new > 0:
                    hunk.lines.append((" ", ""))
                    index += 1
                    continue
            close_hunk()
            index += 1
            continue

        if line.startswith(("old mode ", "new mode ", "deleted file mode ", "new file mode ")):
            if current is not None:
                current.mode_change = line
            index += 1
            continue

        index += 1

    close_hunk()

    if not patches:
        raise DiffParseError("no file headers (--- / +++) found in diff")
    for patch in patches:
        if not patch.hunks and not patch.is_binary and not patch.is_rename:
            raise DiffParseError(f"file patch for {patch.target_path} contains no hunks")
    return patches


def _split_keepends(content: str) -> tuple[list[str], bool]:
    """Split into lines without terminators plus a flag for a trailing newline."""
    normalised = content.replace("\r\n", "\n").replace("\r", "\n")
    if normalised == "":
        return [], False
    ends_with_newline = normalised.endswith("\n")
    if ends_with_newline:
        normalised = normalised[:-1]
    return normalised.split("\n"), ends_with_newline


def _find_offset(source: list[str], expected: list[str], start: int) -> int:
    """Locate ``expected`` in ``source`` near ``start``; return the index or -1."""
    if not expected:
        return max(0, min(start, len(source)))
    window = len(expected)
    candidates = [start]
    for delta in range(1, MAX_OFFSET + 1):
        candidates.append(start - delta)
        candidates.append(start + delta)
    for candidate in candidates:
        if candidate < 0 or candidate + window > len(source):
            continue
        if source[candidate : candidate + window] == expected:
            return candidate
    return -1


def apply_file_patch(original: str | None, patch: FilePatch) -> str | None:
    """Apply one file patch. ``None`` in means "file does not exist"; ``None`` out
    means "file was deleted"."""
    if patch.is_binary:
        raise DiffApplyError(f"binary patches are not supported ({patch.target_path})")

    if patch.is_new_file:
        if original not in (None, ""):
            raise DiffApplyError(f"{patch.new_path} already exists but the diff creates it")
        lines: list[str] = []
        had_newline = True
    else:
        if original is None:
            raise DiffApplyError(f"{patch.old_path} does not exist in the repository")
        lines, had_newline = _split_keepends(original)

    if patch.is_deletion:
        return None

    result: list[str] = []
    cursor = 0
    no_newline_at_end = False

    for hunk in patch.hunks:
        expected = hunk.old_lines()
        target = max(0, hunk.old_start - 1)
        position = _find_offset(lines, expected, target)
        if position < 0:
            preview = expected[0][:80] if expected else "<empty>"
            raise DiffApplyError(
                f"hunk @@ -{hunk.old_start},{hunk.old_count} @@ in {patch.target_path} "
                f"does not match the file (looked for {preview!r} near line {hunk.old_start})"
            )
        if position < cursor:
            raise DiffApplyError(
                f"overlapping or out-of-order hunks in {patch.target_path} "
                f"(@@ -{hunk.old_start} @@)"
            )
        result.extend(lines[cursor:position])
        result.extend(hunk.new_lines())
        cursor = position + len(expected)
        no_newline_at_end = hunk.no_newline_new and cursor >= len(lines)

    result.extend(lines[cursor:])

    if not result:
        return ""
    trailing = had_newline if not patch.is_new_file else True
    if no_newline_at_end:
        trailing = False
    return "\n".join(result) + ("\n" if trailing else "")


@dataclass(slots=True)
class ApplyReport:
    applied: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def touched(self) -> list[str]:
        paths = list(self.applied) + list(self.created) + list(self.deleted)
        paths.extend(new for _, new in self.renamed)
        return sorted(set(paths))


def _safe_join(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root``, refusing traversal and absolute paths."""
    pure = PurePosixPath(relative)
    if pure.is_absolute() or relative.startswith("/") or re.match(r"^[A-Za-z]:", relative):
        raise DiffApplyError(f"absolute paths are not allowed in patches: {relative}")
    if ".." in pure.parts:
        raise DiffApplyError(f"path traversal is not allowed in patches: {relative}")
    resolved = (root / Path(*pure.parts)).resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise DiffApplyError(f"patch escapes the workspace root: {relative}")
    return resolved


def apply_patch_to_tree(root: Path, diff: str) -> ApplyReport:
    """Apply a unified diff to a directory on disk. All-or-nothing.

    Every file is computed in memory first; nothing is written until every hunk
    has applied, so a failure halfway through cannot leave a half-patched tree.
    """
    patches = parse_unified_diff(diff)
    report = ApplyReport()
    # Pending content per file, so that two patch sections targeting the same file
    # compose instead of the second one silently discarding the first.
    pending: dict[Path, str | None] = {}
    order: list[Path] = []

    def current(path: Path) -> str | None:
        if path in pending:
            return pending[path]
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None

    def stage(path: Path, content: str | None) -> None:
        if path not in pending:
            order.append(path)
        pending[path] = content

    for patch in patches:
        source_path = _safe_join(root, patch.old_path) if not patch.is_new_file else None
        dest_path = _safe_join(root, patch.new_path) if not patch.is_deletion else None

        original: str | None = None
        if source_path is not None:
            original = current(source_path)
            if original is None:
                raise DiffApplyError(f"{patch.old_path} does not exist in the repository")

        updated = apply_file_patch(original, patch)

        if updated is None:
            if source_path is not None:
                stage(source_path, None)
                report.deleted.append(patch.old_path)
            continue

        assert dest_path is not None
        stage(dest_path, updated)
        if patch.is_new_file:
            report.created.append(patch.new_path)
        elif patch.is_rename:
            report.renamed.append((patch.old_path, patch.new_path))
            if source_path is not None and source_path != dest_path:
                stage(source_path, None)
        elif patch.new_path not in report.applied:
            report.applied.append(patch.new_path)

    for path in order:
        content = pending[path]
        if content is None:
            if path.exists():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")

    return report


def make_unified_diff(path: str, before: str, after: str, context: int = 3) -> str:
    """Build a git-style unified diff, used by fixtures and the mock model."""
    import difflib

    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    diff = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context,
    )
    body = "".join(diff)
    if not body:
        return ""
    return f"diff --git a/{path} b/{path}\n{body}"
