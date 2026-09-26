"""What an API request is allowed to ask for.

The run form and the API accept execution settings -- a retry budget, sandbox
limits, a network mode, a sandbox image. Without a policy those values would be
the caller's to choose outright: the operator's ``PATCHPILOT_SANDBOX_*`` settings
would be defaults a request could simply overwrite, and a caller could, for
example, give generated code network access on a server configured without it.

Here the server's configuration decides. A field left out of the request takes
the configured default. A field that is sent may tighten the policy freely; it
may loosen it only within the configured caps, and only where the operator has
allowed per-run overrides (``PATCHPILOT_ALLOW_RUN_OVERRIDES``, which defaults to
allowed locally and refused in production). Every violation is reported at
once, as a 422, before anything is queued.
"""

from __future__ import annotations

from pathlib import Path

from patchpilot_agent import build_adapter
from patchpilot_agent.ingest import is_local_source, local_path_for
from patchpilot_core.config import Settings
from patchpilot_core.errors import ConfigurationError, PolicyError, ValidationError
from patchpilot_core.models import RunConfig

from .schemas import RunCreate

OVERRIDE_REMEDIATION = (
    "Leave the field out to use this server's default, or ask the operator to set "
    "PATCHPILOT_ALLOW_RUN_OVERRIDES=true."
)


def check_repository_source(url: str, settings: Settings) -> None:
    """Refuse host paths unless the operator allows them, and then only inside roots.

    A local path is copied, indexed and shown back through the retrieval trace,
    so accepting any path would let an API caller read any directory the server
    can read.
    """
    if not is_local_source(url):
        return
    if not settings.local_repositories_allowed:
        raise PolicyError(
            "this server does not accept local repository paths",
            remediation=(
                "Use an http(s) or ssh git URL, or ask the operator to set "
                "PATCHPILOT_ALLOW_LOCAL_REPOSITORIES=true."
            ),
            context={"repository_url": url},
        )
    roots = settings.local_repository_roots
    if not roots:
        return
    resolved = local_path_for(url).resolve()
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise PolicyError(
            "local repository paths must be inside one of the configured roots",
            remediation="Place the repository under PATCHPILOT_LOCAL_REPOSITORY_ROOTS.",
            context={"repository_url": url, "roots": [str(root) for root in roots]},
        )


def check_model(model: str, settings: Settings) -> None:
    """Fail at creation, not minutes later in the worker, for a model that cannot run."""
    try:
        build_adapter(model, settings)
    except ConfigurationError as exc:
        raise ValidationError(
            f"model {model!r} cannot be used: {exc.message}",
            remediation=exc.remediation,
            context={"model": model},
        ) from exc


def build_run_config(payload: RunCreate, settings: Settings) -> RunConfig:
    """Resolve a request against the server's defaults and enforce its caps."""
    violations: list[str] = []
    overrides = settings.run_overrides_allowed

    def cap(name: str, requested: float | None, limit: float, unit: str = "") -> None:
        if requested is not None and requested > limit:
            violations.append(f"{name}={requested} exceeds this server's maximum of {limit}{unit}")

    def override(name: str, requested: object, reason: str) -> None:
        if not overrides:
            violations.append(f"{name}={requested!r} is refused: {reason}")

    # Hard caps: always enforced, whatever the override setting.
    cap("max_repair_attempts", payload.max_repair_attempts, settings.max_repair_attempts)
    cap("timeout_seconds", payload.timeout_seconds, settings.sandbox_max_timeout_seconds, "s")
    cap("memory_mb", payload.memory_mb, settings.sandbox_max_memory_mb, " MB")
    cap("cpus", payload.cpus, settings.sandbox_max_cpus)

    # Loosening the configured safety policy: allowed only as an override.
    if payload.network == "bridge" and settings.sandbox_network != "bridge":
        override("network", payload.network, "the sandbox is configured without network access")
    if payload.sandbox_image and payload.sandbox_image != settings.sandbox_image:
        override("sandbox_image", payload.sandbox_image, "only the configured image may be used")
    if payload.allowed_write_globs:
        override(
            "allowed_write_globs",
            payload.allowed_write_globs,
            "per-run exceptions to the protected-path policy are disabled",
        )
    if payload.max_patch_files is not None and payload.max_patch_files > settings.max_patch_files:
        override("max_patch_files", payload.max_patch_files, "it exceeds the configured budget")
    if payload.max_patch_lines is not None and payload.max_patch_lines > settings.max_patch_lines:
        override("max_patch_lines", payload.max_patch_lines, "it exceeds the configured budget")
    if payload.sandbox_backend == "local":
        if not settings.local_sandbox_permitted:
            violations.append(
                "sandbox_backend='local' is refused: the unisolated local sandbox is disabled "
                "on this server"
            )
        elif settings.sandbox_backend != "local":
            override("sandbox_backend", payload.sandbox_backend, "it is weaker than configured")

    if violations:
        raise PolicyError(
            "the run request exceeds this server's policy",
            remediation=OVERRIDE_REMEDIATION,
            context={"violations": violations},
        )

    model = payload.model or settings.default_model
    check_model(model, settings)

    return RunConfig(
        model=model,
        temperature=payload.temperature,
        max_repair_attempts=payload.max_repair_attempts or settings.max_repair_attempts,
        setup_command=payload.setup_command,
        baseline_command=payload.baseline_command,
        validation_command=payload.validation_command,
        lint_command=payload.lint_command,
        typecheck_command=payload.typecheck_command,
        reproduction_command=payload.reproduction_command,
        retrieval_top_k=payload.retrieval_top_k or settings.retrieval_top_k,
        max_patch_files=payload.max_patch_files or settings.max_patch_files,
        max_patch_lines=payload.max_patch_lines or settings.max_patch_lines,
        sandbox_backend=payload.sandbox_backend,
        sandbox_image=payload.sandbox_image,
        allowed_write_globs=list(payload.allowed_write_globs),
        limits=settings.sandbox_limits(
            cpus=payload.cpus,
            memory_mb=payload.memory_mb,
            timeout_seconds=payload.timeout_seconds,
            network=payload.network,
        ),
    )


def dataset_path(name: str, directory: Path) -> Path:
    """Resolve a dataset *name* to a file inside the datasets directory.

    ``load_dataset`` also accepts arbitrary paths, which is right for the CLI and
    wrong for an HTTP caller: a dataset names repositories and the shell commands
    to run against them.
    """
    if not name or name != Path(name).name or name.startswith("."):
        raise ValidationError(
            f"dataset {name!r} must be a dataset name, not a path",
            remediation="List the available datasets with GET /api/v1/datasets.",
        )
    stem = Path(name).stem if Path(name).suffix in (".yaml", ".yml", ".json") else name
    for suffix in (".yaml", ".yml", ".json"):
        candidate = directory / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    raise ValidationError(
        f"dataset {name!r} was not found",
        remediation="List the available datasets with GET /api/v1/datasets.",
    )
