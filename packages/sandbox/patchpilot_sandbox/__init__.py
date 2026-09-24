"""Sandboxed execution of untrusted, model-generated code."""

from __future__ import annotations

from .base import BASE_ENV, FORBIDDEN_ENV_SUBSTRINGS, build_env, capture
from .docker_sandbox import DockerSandbox, docker_diagnostics
from .factory import SandboxSelection, build_docker_sandbox, select_sandbox
from .local_sandbox import LocalSubprocessSandbox
from .workspace import (
    COPY_EXCLUDES,
    WorkspaceInfo,
    copy_snapshot,
    disposable_workspace,
    dispose,
    inspect,
)

__all__ = [
    "BASE_ENV",
    "COPY_EXCLUDES",
    "FORBIDDEN_ENV_SUBSTRINGS",
    "DockerSandbox",
    "LocalSubprocessSandbox",
    "SandboxSelection",
    "WorkspaceInfo",
    "build_docker_sandbox",
    "build_env",
    "capture",
    "copy_snapshot",
    "disposable_workspace",
    "dispose",
    "docker_diagnostics",
    "inspect",
    "select_sandbox",
]
