"""Health, system information, sandbox status and the job queue."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from patchpilot_core.config import get_settings
from patchpilot_core.enums import JobStatus
from patchpilot_sandbox import SandboxSelection, select_sandbox
from sqlalchemy.orm import Session

from .. import store
from ..db import get_db
from ..schemas import JobResponse, SandboxStatusResponse, SystemInfoResponse
from ..version import __version__

router = APIRouter(tags=["system"])


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


@router.get("/health", summary="Liveness probe", include_in_schema=False)
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
    selection = select_sandbox(settings)
    queued = sum(
        1
        for job in store.list_jobs(session, limit=500)
        if job.status in (str(JobStatus.QUEUED), str(JobStatus.RUNNING))
    )
    from ..worker import get_worker

    return SystemInfoResponse(
        version=__version__,
        environment=settings.environment,
        database=settings.database_url.split("://", 1)[0],
        vector_store="qdrant" if settings.qdrant_url else "local",
        embedding_provider=settings.embedding_provider,
        default_model=settings.default_model,
        max_repair_attempts=settings.max_repair_attempts,
        sandbox=_sandbox_status(selection),
        worker_running=get_worker(settings).running,
        queued_jobs=queued,
    )


@router.get(
    "/system/sandbox",
    response_model=SandboxStatusResponse,
    summary="Sandbox backend and the controls it applies",
)
def sandbox() -> SandboxStatusResponse:
    return _sandbox_status(select_sandbox(get_settings()))


@router.get("/jobs", response_model=list[JobResponse], summary="Background job queue")
def jobs(
    limit: int = Query(default=50, ge=1, le=500), session: Session = Depends(get_db)
) -> list[JobResponse]:
    return [JobResponse.model_validate(job) for job in store.list_jobs(session, limit)]


@router.get("/jobs/{job_id}", response_model=JobResponse, summary="Job detail")
def job_detail(job_id: str, session: Session = Depends(get_db)) -> JobResponse:
    return JobResponse.model_validate(store.get_job(session, job_id))
