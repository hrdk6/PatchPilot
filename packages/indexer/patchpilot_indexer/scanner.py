"""Repository file discovery.

The scanner decides what the agent is even allowed to see. It honours
``.gitignore``, a default deny-list of dependency/build/secret paths, a size
ceiling and binary detection, and it records *why* each file was skipped so the
indexing report is auditable rather than a silent filter.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .gitignore import GitignoreMatcher

# Directories and files that are never useful to index and are frequently huge or
# sensitive. These apply on top of .gitignore, because plenty of repositories
# commit their virtualenv or .env by accident.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git/",
    ".hg/",
    ".svn/",
    "node_modules/",
    "bower_components/",
    "vendor/",
    ".venv/",
    "venv/",
    "env/",
    "virtualenv/",
    "__pycache__/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".tox/",
    ".nox/",
    ".eggs/",
    "*.egg-info/",
    "site-packages/",
    "dist/",
    "build/",
    "out/",
    "target/",
    ".next/",
    ".nuxt/",
    ".svelte-kit/",
    "coverage/",
    "htmlcov/",
    ".coverage",
    ".idea/",
    ".vscode/",
    ".DS_Store",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
    ".env",
    ".env.*",
    "!.env.example",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "id_rsa",
    "id_ed25519",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
)

LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".md": "markdown",
    ".rst": "restructuredtext",
    ".txt": "text",
    ".toml": "toml",
    ".cfg": "ini",
    ".ini": "ini",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".sh": "shell",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
}

# Files that describe how the project expects to be worked on.
CONVENTION_FILES: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "noxfile.py",
    "Makefile",
    "package.json",
    ".pre-commit-config.yaml",
    "pytest.ini",
    "README.md",
)

BINARY_SNIFF_BYTES = 8192


@dataclass(slots=True)
class ScannedFile:
    path: str
    """Repository-relative POSIX path."""

    absolute: Path
    size_bytes: int
    language: str
    sha256: str
    line_count: int
    is_test: bool


@dataclass(slots=True)
class ScanResult:
    root: Path
    files: list[ScannedFile] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    scanned: int = 0
    truncated: bool = False
    """True when ``max_files`` cut the scan short."""

    def paths(self) -> list[str]:
        return [item.path for item in self.files]


def detect_language(path: str) -> str:
    return LANGUAGE_BY_SUFFIX.get(Path(path).suffix.lower(), "other")


def looks_like_test(path: str) -> bool:
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    if name.startswith("test_") or name.endswith(("_test.py", ".test.ts", ".test.js", ".spec.ts")):
        return True
    parts = lowered.split("/")
    return any(part in ("tests", "test", "testing", "__tests__") for part in parts[:-1])


def is_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    # Heuristic: a high ratio of non-text bytes means this is not source code.
    text_bytes = bytes(range(32, 127)) + b"\n\r\t\f\b"
    non_text = sum(1 for byte in sample if byte not in text_bytes)
    return non_text / len(sample) > 0.30


class RepositoryScanner:
    """Walks a checkout and yields the files worth indexing."""

    def __init__(
        self,
        *,
        max_file_bytes: int = 400_000,
        max_files: int = 5_000,
        extra_excludes: list[str] | None = None,
        include_languages: set[str] | None = None,
    ) -> None:
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.extra_excludes = extra_excludes or []
        self.include_languages = include_languages

    def scan(self, root: Path) -> ScanResult:
        root = root.resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"{root} is not a directory")

        ignore = GitignoreMatcher.from_repository(
            root, list(DEFAULT_EXCLUDES) + self.extra_excludes
        )
        result = ScanResult(root=root)

        for absolute in sorted(root.rglob("*")):
            try:
                relative = absolute.relative_to(root).as_posix()
            except ValueError:  # pragma: no cover - defensive
                continue

            if absolute.is_symlink():
                result.skipped["symlink"] += 1
                continue
            if absolute.is_dir():
                continue

            result.scanned += 1
            if ignore.match(relative, is_dir=False):
                result.skipped["ignored"] += 1
                continue

            try:
                size = absolute.stat().st_size
            except OSError:
                result.skipped["unreadable"] += 1
                continue

            if size > self.max_file_bytes:
                result.skipped["too-large"] += 1
                continue
            if size == 0:
                result.skipped["empty"] += 1
                continue

            try:
                raw = absolute.read_bytes()
            except OSError:
                result.skipped["unreadable"] += 1
                continue

            if is_binary(raw[:BINARY_SNIFF_BYTES]):
                result.skipped["binary"] += 1
                continue

            language = detect_language(relative)
            if self.include_languages and language not in self.include_languages:
                result.skipped[f"language:{language}"] += 1
                continue

            text = raw.decode("utf-8", errors="replace")
            result.files.append(
                ScannedFile(
                    path=relative,
                    absolute=absolute,
                    size_bytes=size,
                    language=language,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    line_count=text.count("\n") + (0 if text.endswith("\n") else 1),
                    is_test=looks_like_test(relative),
                )
            )

            if len(result.files) >= self.max_files:
                result.truncated = True
                break

        return result


def read_convention_files(root: Path, limit_chars: int = 4_000) -> dict[str, str]:
    """Collect the project conventions worth putting in front of the model."""
    conventions: dict[str, str] = {}
    for name in CONVENTION_FILES:
        candidate = root / name
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        conventions[name] = text[:limit_chars]
    return conventions
