"""Text hygiene helpers: truncation, secret redaction, cheap token estimation.

Sandbox output is untrusted and can be enormous. Everything that crosses the
boundary from a sandbox into a prompt, a log line or the database goes through
:func:`sanitize_output` first.
"""

from __future__ import annotations

import re

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "[redacted:openai-key]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"), "[redacted:anthropic-key]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"), "[redacted:github-token]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[redacted:github-token]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[redacted:aws-key]"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1[redacted]"),
    (
        re.compile(r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY)[A-Z0-9_]*)\s*=\s*\S+"),
        r"\1=[redacted]",
    ),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "[redacted:private-key]",
    ),
)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def redact_secrets(text: str) -> str:
    """Best-effort redaction. Defence in depth, not a guarantee -- see docs/security.md."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def mask_paths(text: str, *replacements: tuple[str, str]) -> str:
    """Replace absolute host paths with stable placeholders, both slash styles."""
    for needle, placeholder in replacements:
        if not needle:
            continue
        for variant in {needle, needle.replace("\\", "/"), needle.replace("/", "\\")}:
            text = text.replace(variant, placeholder)
    return text


def truncate_middle(
    text: str, limit: int, marker: str = "\n...[{dropped} bytes omitted]...\n"
) -> tuple[str, int]:
    """Keep the head and tail of ``text``. Failures usually live at both ends."""
    if limit <= 0 or len(text) <= limit:
        return text, 0
    dropped = len(text) - limit
    note = marker.format(dropped=dropped)
    keep = max(limit - len(note), 0)
    head = keep // 3
    tail = keep - head
    return text[:head] + note + (text[-tail:] if tail else ""), dropped


def truncate_tail(text: str, limit: int) -> tuple[str, int]:
    if limit <= 0 or len(text) <= limit:
        return text, 0
    dropped = len(text) - limit
    return f"...[{dropped} bytes omitted]...\n" + text[-limit:], dropped


def sanitize_output(
    text: str, limit: int = 64_000, *path_masks: tuple[str, str]
) -> tuple[str, int]:
    """Full pipeline applied to sandbox stdout/stderr before storage or prompting."""
    cleaned = strip_ansi(text.replace("\x00", ""))
    cleaned = mask_paths(cleaned, *path_masks)
    cleaned = redact_secrets(cleaned)
    return truncate_middle(cleaned, limit)


def estimate_tokens(text: str) -> int:
    """Provider-agnostic token estimate (~4 characters per token).

    Only used when a provider does not report usage. Anything derived from this
    is labelled an estimate in the UI and in benchmark reports.
    """
    if not text:
        return 0
    return max(1, round(len(text) / 4))


def summarize_failure(text: str, max_lines: int = 60, max_chars: int = 4_000) -> str:
    """Compress a test failure into the lines that actually carry signal.

    Keeps assertion/error/traceback lines and pytest summary sections, drops
    progress noise. Falls back to the tail of the output when nothing matches.
    """
    if not text:
        return ""
    interesting = re.compile(
        r"(?i)(error|assert|exception|traceback|failed|fail(?:ure)?s?\b|"
        r"^E\s|^>\s|\.py:\d+|short test summary|expected|actual)"
    )
    lines = strip_ansi(text).splitlines()
    kept: list[str] = []
    for line in lines:
        if interesting.search(line):
            kept.append(line.rstrip())
        if len(kept) >= max_lines:
            break
    if not kept:
        kept = [line.rstrip() for line in lines[-max_lines:]]
    joined = "\n".join(kept)
    result, _ = truncate_tail(joined, max_chars)
    return result


def extract_paths(text: str, known_paths: list[str]) -> list[str]:
    """Return repository paths mentioned in free text (issue body, failure output)."""
    if not text:
        return []
    normalised = text.replace("\\", "/")
    hits: list[str] = []
    for path in known_paths:
        if path in normalised:
            hits.append(path)
            continue
        base = path.rsplit("/", 1)[-1]
        if len(base) > 3 and re.search(rf"(?<![\w/]){re.escape(base)}(?![\w])", normalised):
            hits.append(path)
    return hits
