"""Local subprocess "sandbox" -- for trusted fixtures and CI only.

**This backend does not isolate anything.** It runs commands as the current user,
on the host, with access to the host filesystem outside the workspace. It exists
for exactly two reasons:

1. CI and contributor machines can exercise the whole pipeline without a Docker
   daemon, using the fixture repositories in this repo, whose contents we wrote.
2. It gives the Docker backend something to be differentially tested against.

It is refused when ``PATCHPILOT_ENVIRONMENT=production`` and when
``PATCHPILOT_ALLOW_LOCAL_SANDBOX=false``. Never point it at a repository you did
not write. ``docs/security.md`` says the same thing at more length.

What it *does* provide: a disposable copied workspace, a scrubbed environment
(no host variables, no credentials), wall-clock timeouts that kill the whole
process tree, output truncation with secret redaction, and -- on POSIX only --
address-space, CPU-time, process-count and file-size rlimits.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from patchpilot_core.enums import SandboxStatus
from patchpilot_core.logging import get_logger
from patchpilot_core.models import CommandResult, CommandSpec, SandboxExecution, SandboxLimits

from .base import build_env, capture, make_result

logger = get_logger(__name__, component="sandbox", backend="local")

IS_POSIX = os.name == "posix"

_warned_unisolated = False

# ``sys.platform`` (rather than ``os.name``) is what a type checker narrows on, so
# this import is understood to be POSIX-only rather than merely conditional.
if sys.platform != "win32":  # pragma: no cover - platform dependent
    import resource


def _rlimit_preexec(limits: SandboxLimits) -> Callable[[], None] | None:
    """Return a ``preexec_fn`` applying rlimits, or ``None`` where they do not exist."""
    if sys.platform == "win32":
        return None

    memory_bytes = limits.memory_mb * 1024 * 1024
    cpu_seconds = max(1, int(limits.timeout_seconds))

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))
        resource.setrlimit(resource.RLIMIT_NPROC, (limits.pids, limits.pids))
        resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        # A new session makes the shell the leader of its own process group, so a
        # timeout can kill everything it started with a single killpg.
        os.setsid()

    return apply


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill a timed-out command and everything it spawned.

    Killing only the direct child is not enough: with ``shell=True`` that child
    is ``/bin/sh`` or ``cmd.exe``, and on Windows the venv ``python.exe`` is
    itself a launcher for the real interpreter. A surviving grandchild keeps
    running -- an infinite loop the patch introduced, say -- and keeps the output
    pipes open, which would block the read that follows the kill.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()


class LocalSubprocessSandbox:
    """Implements the ``Sandbox`` protocol without isolation. Read the module docstring."""

    name = "local"

    def __init__(self, *, allowed: bool = True, environment: str = "local") -> None:
        self.allowed = allowed
        self.environment = environment

    def available(self) -> tuple[bool, str]:
        if self.environment == "production":
            return False, (
                "the local subprocess sandbox is refused in production; it provides no "
                "isolation. Use the Docker backend or a remote sandbox."
            )
        if not self.allowed:
            return False, (
                "the local subprocess sandbox is disabled (PATCHPILOT_ALLOW_LOCAL_SANDBOX=false)."
            )
        return True, (
            "local subprocess execution: NO isolation, host user, host filesystem. "
            "Trusted repositories only."
        )

    def run(
        self,
        workspace: Path,
        commands: list[CommandSpec],
        *,
        limits: SandboxLimits,
        env: dict[str, str] | None = None,
    ) -> SandboxExecution:
        started_perf = time.perf_counter()
        execution = SandboxExecution(backend=self.name, image=None, limits=limits)

        usable, reason = self.available()
        if not usable:
            execution.status = SandboxStatus.UNAVAILABLE
            execution.error = reason
            return execution

        global _warned_unisolated
        # Loud once per process. Every execution is still recorded on its run as
        # sandbox_isolated=False and shown in the UI, so repeating the warning
        # for each of a benchmark's hundreds of commands would only bury it.
        log = logger.debug if _warned_unisolated else logger.warning
        _warned_unisolated = True
        log(
            "running commands without isolation",
            extra={"workspace": str(workspace), "commands": len(commands)},
        )
        sandbox_env = build_env(env)
        if not IS_POSIX:
            # cmd.exe needs a usable PATH and the interpreter directory to find python.
            sandbox_env["PATH"] = os.environ.get("PATH", "")
            sandbox_env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
            sandbox_env["PATHEXT"] = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        sandbox_env.setdefault("PYTHONPATH", str(workspace))
        masks = ((str(workspace), "<workspace>"),)

        for spec in commands:
            result = self._exec(workspace, spec, limits, sandbox_env, masks)
            execution.results.append(result)
            if result.status is not SandboxStatus.COMPLETED:
                execution.status = result.status
                execution.error = f"{spec.kind} command did not complete: {result.status}"
                break
            if result.exit_code != 0 and not spec.allow_failure:
                break

        execution.duration_ms = int((time.perf_counter() - started_perf) * 1000)
        return execution

    def _exec(
        self,
        workspace: Path,
        spec: CommandSpec,
        limits: SandboxLimits,
        env: dict[str, str],
        masks: tuple[tuple[str, str], ...],
    ) -> CommandResult:
        timeout = spec.timeout_seconds or limits.timeout_seconds
        started = time.perf_counter()
        command = self._resolve_interpreter(spec.command)
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=str(workspace),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Output is untrusted bytes; a stray invalid sequence must not
                # turn a finished command into a crash in the reader thread.
                encoding="utf-8",
                errors="replace",
                preexec_fn=_rlimit_preexec(limits),
            )
        except OSError as exc:
            return make_result(
                spec.kind,
                spec.command,
                exit_code=None,
                status=SandboxStatus.INTERNAL_ERROR,
                captured=capture("", str(exc), limits, masks),
                started=started,
            )

        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(process)
            try:
                partial_out, partial_err = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
                partial_out, partial_err = "", ""
            logger.warning(
                "sandbox command timed out",
                extra={"kind": str(spec.kind), "timeout_seconds": timeout},
            )
            notice = f"command exceeded the {timeout}s wall-clock limit and was killed"
            return make_result(
                spec.kind,
                spec.command,
                exit_code=None,
                status=SandboxStatus.TIMEOUT,
                captured=capture(
                    partial_out or "",
                    f"{partial_err or ''}\n{notice}".strip(),
                    limits,
                    masks,
                ),
                started=started,
            )

        captured = capture(stdout, stderr, limits, masks)
        return make_result(
            spec.kind,
            spec.command,
            exit_code=process.returncode,
            status=SandboxStatus.COMPLETED,
            captured=captured,
            started=started,
        )

    @staticmethod
    def _resolve_interpreter(command: str) -> str:
        """Map a bare ``python`` to the interpreter running PatchPilot.

        Without this the local backend picks up whatever ``python`` happens to be
        first on PATH, which is rarely the environment the caller meant.
        """
        for prefix in ("python ", "python3 "):
            if command.startswith(prefix):
                return f'"{sys.executable}" ' + command[len(prefix) :]
        return command

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "isolation": "none",
            "network": "unrestricted (host network)",
            "user": "the user running PatchPilot",
            "filesystem": "full host access",
            "rlimits": "POSIX only" if IS_POSIX else "unavailable on this platform",
            "intended_use": "trusted fixture repositories and CI",
        }
