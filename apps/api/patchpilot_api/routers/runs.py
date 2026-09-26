"""Agent run endpoints: creation, state, events, attempts, patches, artifacts."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from patchpilot_agent import resolve_issue
from patchpilot_core.config import get_settings
from patchpilot_core.enums import JobType, RunStatus, StopReason
from patchpilot_core.errors import ConflictError
from sqlalchemy.orm import Session

from .. import store
from ..db import get_db
from ..policy import build_run_config, check_repository_source
from ..schemas import (
    ArtifactResponse,
    AttemptResponse,
    IssueResponse,
    RepositoryResponse,
    RunCreate,
    RunDetailResponse,
    RunEventResponse,
    RunListResponse,
    RunSummaryResponse,
    retrieval_from_context,
)
from ..services import create_run

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post(
    "",
    response_model=RunSummaryResponse,
    status_code=202,
    summary="Create and queue an agent run",
    description=(
        "Returns immediately with a queued run. Indexing and sandboxed execution "
        "happen in the background worker; poll `/runs/{id}/events` for progress."
    ),
)
def create(payload: RunCreate, session: Session = Depends(get_db)) -> RunSummaryResponse:
    settings = get_settings()
    # Policy first: it is cheap and local, whereas resolving an issue number may
    # call the GitHub API.
    check_repository_source(payload.repository_url, settings)
    config = build_run_config(payload, settings)
    issue = resolve_issue(
        payload.repository_url,
        issue_number=payload.issue_number,
        issue_text=payload.issue_text,
        issue_title=payload.issue_title,
        settings=settings,
    )

    run_identifier = create_run(
        repository=payload.to_repository_spec(),
        issue=issue,
        config=config,
        settings=settings,
    )
    # The run row was created in another transaction; read it back for the response.
    session.expire_all()
    return RunSummaryResponse.model_validate(store.get_run(session, run_identifier))


@router.get("", response_model=RunListResponse, summary="List runs")
def index(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, description="Filter by terminal or live status"),
    model: str | None = Query(default=None),
    repository_id: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> RunListResponse:
    rows, total = store.list_runs(
        session,
        limit=limit,
        offset=offset,
        status=status,
        model=model,
        repository_id=repository_id,
    )
    return RunListResponse(
        items=[RunSummaryResponse.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{run_id}",
    response_model=RunDetailResponse,
    summary="Full run detail",
    description=(
        "Everything the dashboard shows for one run: the state-machine timeline, "
        "the retrieval trace with per-chunk scores and reasons, every plan and "
        "diff, sandbox output, and the token/cost/latency breakdown."
    ),
)
def detail(run_id: str, session: Session = Depends(get_db)) -> RunDetailResponse:
    run = store.get_run(session, run_id)
    repository = store.get_repository(session, run.repository_id)
    issue = store.get_issue(session, run.issue_id)
    retrieval, truncated, conventions = retrieval_from_context(run.context_package)
    return RunDetailResponse(
        run=RunSummaryResponse.model_validate(run),
        repository=RepositoryResponse.model_validate(repository),
        issue=IssueResponse.model_validate(issue),
        config=run.config or {},
        commands=run.commands or {},
        latency=run.latency or {},
        baseline=run.baseline,
        final_patch=run.final_patch,
        events=[
            RunEventResponse.model_validate(event) for event in store.list_events(session, run_id)
        ],
        attempts=[
            AttemptResponse.model_validate(attempt)
            for attempt in store.list_attempts(session, run_id)
        ],
        artifacts=[
            ArtifactResponse.model_validate(artifact)
            for artifact in store.list_artifacts(session, run_id)
        ],
        retrieval=retrieval,
        retrieval_truncated=truncated,
        conventions=conventions,
    )


@router.get(
    "/{run_id}/events",
    response_model=list[RunEventResponse],
    summary="State transitions",
    description=(
        "Append-only transition log. Pass `after` with the highest index you have "
        "already seen to poll for new transitions only."
    ),
)
def events(
    run_id: str,
    after: int = Query(default=-1, description="Return transitions with a higher index"),
    session: Session = Depends(get_db),
) -> list[RunEventResponse]:
    store.get_run(session, run_id)
    return [
        RunEventResponse.model_validate(event)
        for event in store.list_events(session, run_id, after=after)
    ]


@router.get("/{run_id}/attempts", response_model=list[AttemptResponse], summary="Repair attempts")
def attempts(run_id: str, session: Session = Depends(get_db)) -> list[AttemptResponse]:
    store.get_run(session, run_id)
    return [
        AttemptResponse.model_validate(attempt) for attempt in store.list_attempts(session, run_id)
    ]


@router.get(
    "/{run_id}/patches",
    summary="Every diff proposed in this run",
    description="Ordered by attempt, including the ones that were rejected and why.",
)
def patches(run_id: str, session: Session = Depends(get_db)) -> list[dict]:
    store.get_run(session, run_id)
    return [
        {
            "attempt": attempt.attempt,
            "diff": attempt.diff,
            "diff_hash": attempt.diff_hash,
            "accepted": bool(attempt.validation and attempt.validation.get("valid")),
            "rejections": (attempt.validation or {}).get("rejections", []),
            "messages": (attempt.validation or {}).get("messages", []),
            "succeeded": attempt.succeeded,
        }
        for attempt in store.list_attempts(session, run_id)
    ]


@router.get(
    "/{run_id}/patch.diff",
    summary="Download the final diff",
    response_class=Response,
    responses={200: {"content": {"text/x-diff": {}}}},
)
def final_patch(run_id: str, session: Session = Depends(get_db)) -> Response:
    run = store.get_run(session, run_id)
    if not run.final_patch:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": "this run produced no patch",
                "remediation": "Check the run status and the rejection reasons.",
            },
        )
    return Response(
        content=run.final_patch,
        media_type="text/x-diff",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.diff"'},
    )


@router.get(
    "/{run_id}/artifacts", response_model=list[ArtifactResponse], summary="Preserved artifacts"
)
def artifacts(run_id: str, session: Session = Depends(get_db)) -> list[ArtifactResponse]:
    store.get_run(session, run_id)
    return [
        ArtifactResponse.model_validate(artifact)
        for artifact in store.list_artifacts(session, run_id)
    ]


@router.post(
    "/{run_id}/cancel",
    response_model=RunSummaryResponse,
    summary="Request cancellation",
    description=(
        "A run no worker has started is cancelled at once. A running one gets a "
        "cancellation flag the state machine checks between nodes; a run already "
        "inside a sandbox command finishes that command first."
    ),
)
def cancel(run_id: str, session: Session = Depends(get_db)) -> RunSummaryResponse:
    run = store.get_run(session, run_id)
    if RunStatus(run.status).is_terminal:
        raise ConflictError(
            f"run is already {run.status}", remediation="Terminal runs cannot be cancelled."
        )
    store.request_cancel(session, run_id)
    jobs = store.active_jobs_for(session, JobType.AGENT_RUN, "run_id", run_id)
    # Nothing is executing it (every job is still queued, or it has none), so
    # there is no state machine to notice the flag: close it here.
    if all(store.cancel_queued_job(session, job.id) for job in jobs):
        store.close_run(
            session,
            run_id,
            status=RunStatus.CANCELLED,
            stop_reason=StopReason.CANCELLED,
            error="cancelled before it started",
        )
    session.flush()
    session.refresh(run)
    return RunSummaryResponse.model_validate(run)


@router.post(
    "/{run_id}/resume",
    response_model=RunSummaryResponse,
    status_code=202,
    summary="Resume an interrupted run",
    description=(
        "Requeues a run that was left mid-flight, for example by a worker restart. "
        "The recorded transition history is preserved and the state machine "
        "continues appending to it. Terminal runs cannot be resumed."
    ),
)
def resume(run_id: str, session: Session = Depends(get_db)) -> RunSummaryResponse:
    run = store.get_run(session, run_id)
    if RunStatus(run.status).is_terminal:
        raise ConflictError(
            f"run is already {run.status}",
            remediation="Create a new run instead; terminal runs are immutable.",
        )
    # A second job for a run that is still queued or executing would run the
    # same state machine twice at once, interleaving two transition logs.
    active = store.active_jobs_for(session, JobType.AGENT_RUN, "run_id", run_id)
    if active:
        raise ConflictError(
            f"run is already {active[0].status} as job {active[0].id}",
            remediation=(
                "Only a run whose job has ended can be resumed. A job whose worker died "
                "is requeued automatically once its lease expires."
            ),
            context={"job_id": active[0].id},
        )
    store.ensure_queue_capacity(session, get_settings())
    run.cancel_requested = False
    run.status = str(RunStatus.QUEUED)
    store.enqueue_job(session, JobType.AGENT_RUN, {"run_id": run_id})
    return RunSummaryResponse.model_validate(run)


artifact_router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@artifact_router.get("/{artifact_id}", response_model=ArtifactResponse, summary="Artifact metadata")
def artifact_detail(artifact_id: str, session: Session = Depends(get_db)) -> ArtifactResponse:
    return ArtifactResponse.model_validate(store.get_artifact(session, artifact_id))


@artifact_router.get(
    "/{artifact_id}/download",
    summary="Download an artifact",
    description="Sanitised sandbox logs and diffs kept after the container was destroyed.",
)
def artifact_download(artifact_id: str, session: Session = Depends(get_db)) -> FileResponse:
    row = store.get_artifact(session, artifact_id)
    path = store.artifact_path(row)
    if not path.is_file():
        raise HTTPException(
            status_code=410,
            detail={
                "code": "gone",
                "message": "the artifact file is no longer on disk",
                "remediation": (
                    "Artifacts live under PATCHPILOT_DATA_DIR; it may have been cleared."
                ),
            },
        )
    return FileResponse(path, media_type=row.media_type, filename=row.filename)
