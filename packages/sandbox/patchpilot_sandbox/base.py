"""Shared plumbing for sandbox backends: environment scrubbing and capture."""

from __future__ import annotations

import time
from dataclasses import dataclass

from patchpilot_core.enums import CommandKind, SandboxStatus
from patchpilot_core.models import CommandResult, SandboxLimits
from patchpilot_core.textutil import sanitize_output

# The sandbox gets this environment and nothing else. Notably absent: PATH-adjacent
# credentials, PATCHPILOT_*, OPENAI_*, ANTHROPIC_*, GITHUB_*, AWS_*, SSH_*, and
# every other variable present on the host.
BASE_ENV: dict[str, str] = {
    "PATH": "/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin",
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
    "CI": "true",
    "NO_COLOR": "1",
    "TERM": "dumb",
}

# Anything matching these is refused even if a caller passes it explicitly.
FORBIDDEN_ENV_SUBSTRINGS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "API_KEY",
    "APIKEY",
    "CREDENTIAL",
    "PRIVATE",
    "SSH",
    "AWS_",
    "GITHUB_",
    "OPENAI_",
    "ANTHROPIC_",
    "DOCKER_",
)


def build_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build the sandbox environment from a fixed base plus vetted extras."""
    env = dict(BASE_ENV)
    for key, value in (extra or {}).items():
        upper = key.upper()
        if any(marker in upper for marker in FORBIDDEN_ENV_SUBSTRINGS):
            continue
        env[key] = value
    return env


@dataclass(slots=True)
class CapturedOutput:
    stdout: str
    stderr: str
    dropped: int
    truncated: bool


def capture(
    stdout: str, stderr: str, limits: SandboxLimits, path_masks: tuple[tuple[str, str], ...] = ()
) -> CapturedOutput:
    """Sanitise and bound the output of one command."""
    half = max(limits.max_output_bytes // 2, 1_000)
    clean_out, dropped_out = sanitize_output(stdout or "", half, *path_masks)
    clean_err, dropped_err = sanitize_output(stderr or "", half, *path_masks)
    dropped = dropped_out + dropped_err
    return CapturedOutput(
        stdout=clean_out, stderr=clean_err, dropped=dropped, truncated=dropped > 0
    )


def make_result(
    kind: CommandKind,
    command: str,
    *,
    exit_code: int | None,
    status: SandboxStatus,
    captured: CapturedOutput,
    started: float,
) -> CommandResult:
    return CommandResult(
        kind=kind,
        command=command,
        exit_code=exit_code,
        status=status,
        stdout=captured.stdout,
        stderr=captured.stderr,
        duration_ms=int((time.perf_counter() - started) * 1000),
        truncated=captured.truncated,
        bytes_dropped=captured.dropped,
    )
