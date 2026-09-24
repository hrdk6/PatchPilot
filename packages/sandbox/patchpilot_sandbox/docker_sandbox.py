"""Docker-backed sandbox.

Controls applied to every attempt:

* the repository is **copied in** with ``docker cp``; no host path is ever bind
  mounted, so the container cannot see or modify the host workspace;
* ``--network none`` by default, so generated code cannot exfiltrate anything or
  pull arbitrary dependencies;
* a non-root user for every command that runs untrusted code;
* ``--cap-drop ALL`` plus only the three capabilities needed to chown the copied
  tree, and those are only usable by root, which untrusted code never is;
* ``--security-opt no-new-privileges`` so a setuid binary cannot escalate;
* memory, swap, CPU and PID limits, a tmpfs ``/tmp`` with a size cap, and an
  optional storage quota where the storage driver supports one;
* wall-clock timeouts at both the command and container level;
* output truncation and secret redaction before anything is stored or prompted.

Known limits, stated plainly: this is kernel-shared isolation. A kernel or
runtime escape defeats it. ``docs/security.md`` describes when to replace this
with a microVM or a hosted sandbox.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from patchpilot_core.enums import SandboxStatus
from patchpilot_core.logging import get_logger
from patchpilot_core.models import (
    CommandResult,
    CommandSpec,
    SandboxExecution,
    SandboxLimits,
)

from .base import build_env, capture, make_result

logger = get_logger(__name__, component="sandbox", backend="docker")

CONTAINER_WORKDIR = "/workspace"
DOCKER_CLI_TIMEOUT = 120
SETUP_GRACE_SECONDS = 60


class DockerSandbox:
    """Implements the ``Sandbox`` protocol using the local Docker daemon."""

    name = "docker"

    def __init__(
        self,
        image: str = "python:3.12-slim-bookworm",
        *,
        docker_binary: str = "docker",
        workdir: str = CONTAINER_WORKDIR,
        tmpfs_size_mb: int = 64,
        storage_quota_gb: int | None = None,
        pull_missing: bool = True,
    ) -> None:
        self.image = image
        self.docker_binary = docker_binary
        self.workdir = workdir
        self.tmpfs_size_mb = tmpfs_size_mb
        self.storage_quota_gb = storage_quota_gb
        self.pull_missing = pull_missing

    # --------------------------------------------------------------- readiness
    def available(self) -> tuple[bool, str]:
        binary = shutil.which(self.docker_binary)
        if binary is None:
            return False, (
                f"{self.docker_binary!r} is not on PATH. Install Docker Desktop or the "
                "Docker Engine, or set PATCHPILOT_SANDBOX_BACKEND=local for trusted "
                "fixtures only."
            )
        try:
            probe = subprocess.run(
                [self.docker_binary, "info", "--format", "{{json .ServerVersion}}"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=DOCKER_CLI_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"could not talk to the Docker daemon: {exc}"
        version = probe.stdout.strip().strip('"')
        # `docker info` exits 0 when the CLI works but the daemon does not -- a
        # stopped Docker Desktop, for one -- and prints an empty server version.
        # Trusting the exit code alone would select a backend that then fails
        # every run at the image pull instead of falling back.
        if probe.returncode != 0 or not version:
            detail = (probe.stderr or probe.stdout).strip().splitlines()
            return False, (
                "the Docker daemon is not reachable: " + (detail[-1] if detail else "unknown error")
            )
        return True, f"docker daemon {version}"

    # ------------------------------------------------------------------- run
    def run(
        self,
        workspace: Path,
        commands: list[CommandSpec],
        *,
        limits: SandboxLimits,
        env: dict[str, str] | None = None,
    ) -> SandboxExecution:
        started_perf = time.perf_counter()
        execution = SandboxExecution(backend=self.name, image=self.image, limits=limits)

        usable, reason = self.available()
        if not usable:
            execution.status = SandboxStatus.UNAVAILABLE
            execution.error = reason
            return execution

        if self.pull_missing and not self._image_present():
            pulled = self._pull_image()
            if not pulled:
                execution.status = SandboxStatus.SETUP_FAILED
                execution.error = (
                    f"image {self.image!r} is not available locally and could not be "
                    "pulled. Pre-pull it, or point PATCHPILOT_SANDBOX_IMAGE at an "
                    "image you already have."
                )
                return execution

        container = f"patchpilot-{uuid.uuid4().hex[:12]}"
        total_budget = (
            sum((spec.timeout_seconds or limits.timeout_seconds) for spec in commands)
            + SETUP_GRACE_SECONDS
        )
        masks = ((str(workspace), "<workspace>"), (self.workdir, "<workspace>"))

        try:
            create = self._start_container(container, limits, total_budget)
            if create is not None:
                execution.status = SandboxStatus.SETUP_FAILED
                execution.error = create
                return execution

            copied = self._copy_workspace(container, workspace, limits)
            if copied is not None:
                execution.status = SandboxStatus.SETUP_FAILED
                execution.error = copied
                return execution

            sandbox_env = build_env(env)
            for spec in commands:
                result = self._exec(container, spec, limits, sandbox_env, masks)
                execution.results.append(result)
                if result.status is not SandboxStatus.COMPLETED:
                    execution.status = result.status
                    execution.error = f"{spec.kind} command did not complete: {result.status}"
                    break
                if result.exit_code != 0 and not spec.allow_failure:
                    # A non-zero exit is a legitimate outcome, not a sandbox failure.
                    break
        finally:
            self._destroy(container)
            execution.duration_ms = int((time.perf_counter() - started_perf) * 1000)

        return execution

    # --------------------------------------------------------------- internals
    def _docker(
        self, *args: str, timeout: int = DOCKER_CLI_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.docker_binary, *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def _image_present(self) -> bool:
        probe = self._docker("image", "inspect", self.image)
        return probe.returncode == 0

    def _pull_image(self) -> bool:
        logger.info("pulling sandbox image", extra={"image": self.image})
        try:
            pull = self._docker("pull", self.image, timeout=600)
        except subprocess.TimeoutExpired:
            return False
        return pull.returncode == 0

    def _start_container(
        self, container: str, limits: SandboxLimits, budget_seconds: int
    ) -> str | None:
        memory = f"{limits.memory_mb}m"
        args = [
            "run",
            "--detach",
            "--name",
            container,
            "--label",
            "patchpilot=sandbox",
            "--network",
            limits.network,
            "--memory",
            memory,
            "--memory-swap",
            memory,  # equal to --memory disables swap entirely
            "--cpus",
            str(limits.cpus),
            "--pids-limit",
            str(limits.pids),
            "--cap-drop",
            "ALL",
            # Only root can use these, and untrusted commands never run as root.
            # They exist so the copied tree can be chowned to the sandbox user.
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "FOWNER",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            f"/tmp:rw,size={self.tmpfs_size_mb}m,mode=1777",
            "--workdir",
            self.workdir,
            "--entrypoint",
            "",
        ]
        if self.storage_quota_gb:
            args += ["--storage-opt", f"size={self.storage_quota_gb}G"]
        args += [self.image, "sleep", str(budget_seconds)]

        try:
            started = self._docker(*args)
        except subprocess.TimeoutExpired:
            return "timed out starting the sandbox container"

        if started.returncode != 0 and self.storage_quota_gb:
            # --storage-opt is only supported on some storage drivers; retry without it
            # rather than failing the run, and say so in the logs.
            logger.warning(
                "storage quota unsupported by this storage driver; continuing without it",
                extra={"detail": started.stderr.strip()[:200]},
            )
            retry_args = [a for a in args if a != "--storage-opt"]
            retry_args = [a for a in retry_args if not a.startswith("size=")]
            started = self._docker(*retry_args)

        if started.returncode != 0:
            return f"could not start the sandbox container: {started.stderr.strip()[:500]}"
        return None

    def _copy_workspace(self, container: str, workspace: Path, limits: SandboxLimits) -> str | None:
        # Trailing "/." copies the *contents* of the directory.
        copied = self._docker("cp", f"{workspace}/.", f"{container}:{self.workdir}", timeout=300)
        if copied.returncode != 0:
            return f"could not copy the workspace into the sandbox: {copied.stderr.strip()[:500]}"
        chown = self._docker(
            "exec",
            "--user",
            "0:0",
            container,
            "chown",
            "-R",
            limits.user,
            self.workdir,
            timeout=300,
        )
        if chown.returncode != 0:
            return (
                "could not hand the workspace to the unprivileged sandbox user: "
                f"{chown.stderr.strip()[:300]}"
            )
        return None

    def _exec(
        self,
        container: str,
        spec: CommandSpec,
        limits: SandboxLimits,
        env: dict[str, str],
        masks: tuple[tuple[str, str], ...],
    ) -> CommandResult:
        timeout = spec.timeout_seconds or limits.timeout_seconds
        args = ["exec", "--user", limits.user, "--workdir", self.workdir]
        for key, value in env.items():
            args += ["--env", f"{key}={value}"]
        args += [container, "/bin/sh", "-c", spec.command]

        started = time.perf_counter()
        try:
            completed = self._docker(*args, timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning(
                "sandbox command timed out",
                extra={"kind": str(spec.kind), "timeout_seconds": timeout},
            )
            return make_result(
                spec.kind,
                spec.command,
                exit_code=None,
                status=SandboxStatus.TIMEOUT,
                captured=capture(
                    "", f"command exceeded the {timeout}s wall-clock limit", limits, masks
                ),
                started=started,
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

        captured = capture(completed.stdout, completed.stderr, limits, masks)
        # Truncation must not mask the real exit code, so the status stays COMPLETED
        # and ``truncated``/``bytes_dropped`` carry the signal to the UI.
        return make_result(
            spec.kind,
            spec.command,
            exit_code=completed.returncode,
            status=SandboxStatus.COMPLETED,
            captured=captured,
            started=started,
        )

    def _destroy(self, container: str) -> None:
        try:
            self._docker("rm", "--force", "--volumes", container, timeout=60)
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - best effort
            logger.warning("could not remove sandbox container", extra={"container": container})

    # ------------------------------------------------------------------ extras
    def describe(self) -> dict[str, Any]:
        """Machine-readable description of the controls, surfaced by the API."""
        return {
            "backend": self.name,
            "image": self.image,
            "host_mounts": [],
            "network": "none (default)",
            "user": "non-root",
            "capabilities": "ALL dropped except CHOWN/DAC_OVERRIDE/FOWNER (root-only)",
            "no_new_privileges": True,
            "tmpfs": f"/tmp size={self.tmpfs_size_mb}m",
            "docker_socket_mounted": False,
            "host_env_forwarded": False,
        }

    def cleanup_orphans(self) -> int:
        """Remove any container this process left behind (crash recovery)."""
        listed = self._docker("ps", "-aq", "--filter", "label=patchpilot=sandbox")
        ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        for identifier in ids:
            self._docker("rm", "--force", "--volumes", identifier, timeout=60)
        return len(ids)


def docker_diagnostics(binary: str = "docker") -> dict[str, Any]:
    """Best-effort daemon details for the API's ``/system/sandbox`` endpoint."""
    if shutil.which(binary) is None:
        return {"available": False, "reason": "docker binary not found on PATH"}
    probe = subprocess.run(
        [binary, "version", "--format", "{{json .}}"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=DOCKER_CLI_TIMEOUT,
        check=False,
    )
    if probe.returncode != 0:
        return {"available": False, "reason": (probe.stderr or probe.stdout).strip()[:300]}
    try:
        payload = json.loads(probe.stdout)
    except json.JSONDecodeError:
        payload = {"raw": probe.stdout[:300]}
    return {"available": True, "version": payload}


def quote(command: str) -> str:
    """Shell-quote for display in the UI without changing execution semantics."""
    return shlex.quote(command)
