"""A small, dependency-free ``.gitignore`` matcher.

Supports the parts of the spec that matter for indexing a checkout: comments,
blank lines, negation (``!``), anchoring (a leading or embedded ``/``),
directory-only patterns (trailing ``/``), ``*``, ``?``, ``**`` and character
classes. Precedence follows git: the last matching pattern wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class _Rule:
    regex: re.Pattern[str]
    """Matches the entry itself *and* anything beneath it."""

    exact: re.Pattern[str]
    """Matches only the entry itself."""

    negated: bool
    directory_only: bool
    source: str


def _translate(pattern: str) -> str:
    """Translate a gitignore glob body into a regular expression body."""
    out: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if pattern[index : index + 3] == "**/":
                out.append("(?:.*/)?")
                index += 3
                continue
            if pattern[index : index + 2] == "**":
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
            index += 1
            continue
        if char == "?":
            out.append("[^/]")
            index += 1
            continue
        if char == "[":
            end = index + 1
            if end < length and pattern[end] in ("!", "^"):
                end += 1
            if end < length and pattern[end] == "]":
                end += 1
            while end < length and pattern[end] != "]":
                end += 1
            if end >= length:
                out.append(re.escape(char))
                index += 1
                continue
            body = pattern[index + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            index = end + 1
            continue
        out.append(re.escape(char))
        index += 1
    return "".join(out)


def _compile(pattern: str) -> _Rule | None:
    raw = pattern
    line = pattern.rstrip("\n")
    if not line.strip() or line.lstrip().startswith("#"):
        return None
    negated = line.startswith("!")
    if negated:
        line = line[1:]
    # An escaped leading '#' or '!' is a literal.
    if line.startswith(("\\#", "\\!")):
        line = line[1:]
    line = line.rstrip()
    if not line:
        return None

    directory_only = line.endswith("/")
    if directory_only:
        line = line[:-1]

    anchored = line.startswith("/") or ("/" in line[:-1] if line.endswith("/") else "/" in line)
    if line.startswith("/"):
        line = line[1:]

    body = _translate(line)
    prefix = "^" if anchored else "^(?:.*/)?"
    return _Rule(
        regex=re.compile(prefix + body + r"(?:/.*)?$"),
        exact=re.compile(prefix + body + r"/?$"),
        negated=negated,
        directory_only=directory_only,
        source=raw.strip(),
    )


class GitignoreMatcher:
    """Matches repository-relative POSIX paths against a set of ignore patterns."""

    def __init__(self, patterns: list[str] | None = None) -> None:
        self._rules: list[_Rule] = []
        for pattern in patterns or []:
            rule = _compile(pattern)
            if rule is not None:
                self._rules.append(rule)

    @classmethod
    def from_repository(
        cls, root: Path, extra_patterns: list[str] | None = None
    ) -> GitignoreMatcher:
        """Load ``.gitignore`` from the repo root plus any extra patterns.

        Nested ``.gitignore`` files are read too and their patterns re-anchored to
        the repository root, which covers the common monorepo layout.
        """
        patterns: list[str] = list(extra_patterns or [])
        root_ignore = root / ".gitignore"
        if root_ignore.is_file():
            patterns.extend(root_ignore.read_text(encoding="utf-8", errors="replace").splitlines())
        for nested in sorted(root.rglob(".gitignore")):
            if nested == root_ignore or not nested.is_file():
                continue
            relative = nested.parent.relative_to(root).as_posix()
            if relative.startswith(".git/"):
                continue
            for line in nested.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                negated = stripped.startswith("!")
                if negated:
                    stripped = stripped[1:]
                anchored = f"{relative}/{stripped.lstrip('/')}"
                patterns.append(("!" if negated else "") + anchored)
        return cls(patterns)

    def match(self, relative_path: str, *, is_dir: bool = False) -> bool:
        """True when the path should be ignored. The last matching rule wins."""
        path = relative_path.replace("\\", "/").removeprefix("./").lstrip("/")
        ignored = False
        for rule in self._rules:
            if not rule.regex.match(path):
                continue
            if rule.directory_only and not is_dir and rule.exact.match(path):
                # ``build/`` must not ignore a *file* named ``build``, though it does
                # ignore ``build/main.o`` (which the non-exact regex matches).
                continue
            ignored = not rule.negated
        return ignored

    def __len__(self) -> int:
        return len(self._rules)
