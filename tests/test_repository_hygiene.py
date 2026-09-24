"""Repository hygiene: every tracked text file is clean UTF-8.

A Windows PowerShell `echo "# Title" >> README.md` appends UTF-16, which turns
the file into binary as far as git and GitHub are concerned: the README stops
rendering. That happened twice while this repository was being set up, so CI
now refuses it.
"""

from __future__ import annotations

import subprocess

import pytest
from conftest import REPO_ROOT

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".woff", ".woff2"}


def tracked_text_files() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if listed.returncode != 0:
        pytest.skip("not a git checkout")
    return [
        path
        for path in listed.stdout.splitlines()
        if path and not any(path.lower().endswith(suffix) for suffix in BINARY_SUFFIXES)
    ]


def test_tracked_text_files_are_utf8_without_nul_bytes() -> None:
    offenders: list[str] = []
    for relative in tracked_text_files():
        path = REPO_ROOT / relative
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\x00" in data:
            offenders.append(f"{relative}: contains NUL bytes (UTF-16 append?)")
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            offenders.append(f"{relative}: not valid UTF-8 ({exc.reason})")
    assert not offenders, "\n".join(offenders)
