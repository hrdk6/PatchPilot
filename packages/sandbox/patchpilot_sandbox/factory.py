"""Sandbox selection.

``auto`` prefers Docker and falls back to the local subprocess backend only when
that is explicitly allowed. The choice, and the reason for it, is returned to the
caller so it can be recorded on the run and shown in the UI -- a run that used
the unisolated backend must never look like a run that used Docker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from patchpilot_core.config import Settings, get_settings
from patchpilot_core.interfaces import Sandbox
from patchpilot_core.logging import get_logger

from .docker_sandbox import DockerSandbox
from .local_sandbox import LocalSubprocessSandbox

logger = get_logger(__name__, component="sandbox")

_warned_fallback = False


@dataclass(slots=True)
class SandboxSelection:
    sandbox: Sandbox
    backend: str
    available: bool
    reason: str
    isolated: bool

    def describe(self) -> dict[str, Any]:
        details = getattr(self.sandbox, "describe", None)
        return {
            "backend": self.backend,
            "available": self.available,
            "isolated": self.isolated,
            "reason": self.reason,
            "controls": details() if callable(details) else {},
        }


def build_docker_sandbox(settings: Settings, image: str | None = None) -> DockerSandbox:
    return DockerSandbox(
        image or settings.sandbox_image,
        workdir=settings.sandbox_workdir,
        tmpfs_size_mb=settings.sandbox_tmpfs_size_mb,
    )


def select_sandbox(
    settings: Settings | None = None,
    *,
    backend: str = "auto",
    image: str | None = None,
) -> SandboxSelection:
    settings = settings or get_settings()
    requested = backend if backend != "auto" else settings.sandbox_backend

    if requested == "docker":
        docker_only = build_docker_sandbox(settings, image)
        available, reason = docker_only.available()
        return SandboxSelection(docker_only, "docker", available, reason, isolated=True)

    if requested == "local":
        local_only = LocalSubprocessSandbox(
            allowed=settings.allow_local_sandbox, environment=settings.environment
        )
        available, reason = local_only.available()
        return SandboxSelection(local_only, "local", available, reason, isolated=False)

    docker = build_docker_sandbox(settings, image)
    docker_ok, docker_reason = docker.available()
    if docker_ok:
        return SandboxSelection(docker, "docker", True, docker_reason, isolated=True)

    local = LocalSubprocessSandbox(
        allowed=settings.allow_local_sandbox, environment=settings.environment
    )
    local_ok, local_reason = local.available()
    if local_ok:
        global _warned_fallback
        # Once per process is enough to explain why; every run records the
        # backend it actually got, and the reason travels with the selection.
        log = logger.debug if _warned_fallback else logger.warning
        _warned_fallback = True
        log(
            "falling back to the unisolated local sandbox",
            extra={"docker_reason": docker_reason},
        )
        return SandboxSelection(
            local,
            "local",
            True,
            f"Docker unavailable ({docker_reason}); {local_reason}",
            isolated=False,
        )

    return SandboxSelection(
        docker,
        "docker",
        False,
        f"no usable sandbox backend. Docker: {docker_reason} Local: {local_reason}",
        isolated=True,
    )
