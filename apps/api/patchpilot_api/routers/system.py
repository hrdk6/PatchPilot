"""Health, system information, sandbox status and the job queue."""

from __future__ import annotations

import threading
import time

from fastapi import APIRouter, Depends, Query
from patchpilot_core.config import Settings, get_settings
from patchpilot_core.enums import JobStatus
from patchpilot_sandbox import SandboxSelection, select_sandbox
from sqlalchemy.orm import Session

from .. import store
from ..db import get_db
from ..schemas import (
    JobResponse,
    RunPolicyResponse,
    SandboxStatusResponse,
    SystemInfoResponse,
)
from ..version import __version__

router = APIRouter(tags=["system"])
# Probes are unauthenticated so an orchestrator can call them without a key.
public_router = APIRouter(tags=["system"])


# Probing Docker shells out to `docker info`, which can take seconds, and the
# dashboard polls /system. The answer changes rarely, so it is cached briefly.
SANDBOX_STATUS_TTL_SECONDS = 30.0
_sandbox_cache: dict[tuple[str, ...], tuple[float, SandboxStatusResponse]] = {}
_sandbox_cache_lock = threading.Lock()


def _cached_sandbox_status(settings: Settings) -> SandboxStatusResponse:
    key = (
        settings.sandbox_backend,
        settings.sandbox_image,
        settings.environment,
        str(settings.allow_local_sandbox),
    )
    now = time.monotonic()
    with _sandbox_cache_lock:
        cached = _sandbox_cache.get(key)
        if cached is not None and now - cached[0] < SANDBOX_STATUS_TTL_SECONDS:
            return cached[1]
    status = _sandbox_status(select_sandbox(settings))
    with _sandbox_cache_lock:
        _sandbox_cache[key] = (now, status)
    return status


def _sandbox_status(selection: SandboxSelection) -> SandboxStatusResponse:
    """Project a sandbox selection onto the API schema, field by field."""
    described = selection.describe()
    return SandboxStatusResponse(
        backend=selection.backend,
        available=selection.available,
        isolated=selection.isolated,
        reason=selection.reason,
        controls=dict(described.get("controls") or {}),  # type: ignore[arg-type]
    )


@public_router.get("/health", summary="Liveness probe", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get(
    "/system",
    response_model=SystemInfoResponse,
    summary="Runtime configuration and sandbox status",
    description=(
        "What this instance is actually configured to do. The `sandbox` block is "
        "the honest answer to 'is generated code isolated right now?'."
    ),
)
def system(session: Session = Depends(get_db)) -> SystemInfoResponse:
    settings = get_settings()
    queued = store.count_jobs(session, JobStatus.QUEUED, JobStatus.RUNNING)
    from ..worker import get_worker

    return SystemInfoResponse(
        version=__version__,
        environment=settings.environment,
        database=settings.database_url.split("://", 1)[0],
        vector_store="qdrant" if settings.qdrant_url else "local",
        embedding_provider=settings.embedding_provider,
        default_model=settings.default_model,
        max_repair_attempts=settings.max_repair_attempts,
        sandbox=_cached_sandbox_status(settings),
        worker_running=get_worker(settings).running,
        queued_jobs=queued,
        auth_enabled=settings.auth_enabled,
        policy=RunPolicyResponse(
            max_repair_attempts=settings.max_repair_attempts,
            default_timeout_seconds=settings.sandbox_timeout_seconds,
            max_timeout_seconds=settings.sandbox_max_timeout_seconds,
            max_memory_mb=settings.sandbox_max_memory_mb,
            max_cpus=settings.sandbox_max_cpus,
            network=settings.sandbox_network,
            run_overrides_allowed=settings.run_overrides_allowed,
            local_repositories_allowed=settings.local_repositories_allowed,
            local_sandbox_allowed=settings.local_sandbox_permitted,
        ),
    )


@router.get(
    "/system/sandbox",
    response_model=SandboxStatusResponse,
    summary="Sandbox backend and the controls it applies",
)
def sandbox() -> SandboxStatusResponse:
    return _cached_sandbox_status(get_settings())


@router.get("/jobs", response_model=list[JobResponse], summary="Background job queue")
def jobs(
    limit: int = Query(default=50, ge=1, le=500), session: Session = Depends(get_db)
) -> list[JobResponse]:
    return [JobResponse.model_validate(job) for job in store.list_jobs(session, limit)]


@router.get("/jobs/{job_id}", response_model=JobResponse, summary="Job detail")
def job_detail(job_id: str, session: Session = Depends(get_db)) -> JobResponse:
    return JobResponse.model_validate(store.get_job(session, job_id))
