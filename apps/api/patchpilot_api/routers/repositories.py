"""Repository, indexing, issue-lookup and model endpoints."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query
from patchpilot_agent import describe_models, fetch_github_issue
from patchpilot_core.config import get_settings
from patchpilot_core.enums import JobType
from patchpilot_core.errors import NotFoundError
from sqlalchemy.orm import Session

from .. import store
from ..db import get_db
from ..policy import check_repository_source
from ..schemas import (
    DependencyGraphResponse,
    ImportEdgeResponse,
    IndexResponse,
    IssueLookupRequest,
    IssueLookupResponse,
    JobResponse,
    ModelResponse,
    RepositoryCreate,
    RepositoryResponse,
    SymbolResponse,
)

router = APIRouter(prefix="/repositories", tags=["repositories"])


@router.post(
    "", response_model=RepositoryResponse, status_code=201, summary="Register a repository"
)
def create(payload: RepositoryCreate, session: Session = Depends(get_db)) -> RepositoryResponse:
    check_repository_source(payload.url, get_settings())
    row = store.upsert_repository(session, payload.to_spec())
    return RepositoryResponse.model_validate(row)


@router.get("", response_model=list[RepositoryResponse], summary="List repositories")
def index(
    limit: int = Query(default=100, ge=1, le=500), session: Session = Depends(get_db)
) -> list[RepositoryResponse]:
    return [
        RepositoryResponse.model_validate(row) for row in store.list_repositories(session, limit)
    ]


@router.get("/{repository_id}", response_model=RepositoryResponse, summary="Repository detail")
def detail(repository_id: str, session: Session = Depends(get_db)) -> RepositoryResponse:
    return RepositoryResponse.model_validate(store.get_repository(session, repository_id))


@router.post(
    "/{repository_id}/index",
    response_model=JobResponse,
    status_code=202,
    summary="Queue a structural index build",
    description=(
        "Clones or copies the checkout, parses it into symbol chunks, builds the "
        "import graph and populates the vector store. Runs in the background."
    ),
)
def build_index(repository_id: str, session: Session = Depends(get_db)) -> JobResponse:
    store.get_repository(session, repository_id)
    job = store.enqueue_job(session, JobType.INDEX_REPOSITORY, {"repository_id": repository_id})
    return JobResponse.model_validate(job)


@router.get(
    "/{repository_id}/index",
    response_model=IndexResponse,
    summary="Latest index for a repository",
)
def get_index(
    repository_id: str,
    repo_sha: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> IndexResponse:
    store.get_repository(session, repository_id)
    row = store.get_index(session, repository_id, repo_sha)
    if row is None:
        raise NotFoundError(
            f"no index exists for repository {repository_id}",
            remediation="POST to /repositories/{id}/index first.",
        )
    return IndexResponse.model_validate(row)


@router.get(
    "/{repository_id}/symbols",
    response_model=list[SymbolResponse],
    summary="Parsed symbols",
)
def symbols(
    repository_id: str,
    path: str | None = Query(default=None, description="Restrict to one file"),
    session: Session = Depends(get_db),
) -> list[SymbolResponse]:
    row = store.get_index(session, repository_id)
    if row is None:
        raise NotFoundError(f"no index exists for repository {repository_id}")
    return [
        SymbolResponse.model_validate(item) for item in store.index_symbols(session, row.id, path)
    ]


@router.get(
    "/{repository_id}/graph",
    response_model=DependencyGraphResponse,
    summary="Import dependency graph",
    description="Resolved edges point at a file in the repository; unresolved ones are "
    "third-party or standard-library imports and are kept for context.",
)
def graph(repository_id: str, session: Session = Depends(get_db)) -> DependencyGraphResponse:
    row = store.get_index(session, repository_id)
    if row is None:
        raise NotFoundError(f"no index exists for repository {repository_id}")
    edges = store.index_graph(session, row.id)
    nodes = sorted(
        {edge.source_path for edge in edges}
        | {edge.target_path for edge in edges if edge.target_path}
    )
    return DependencyGraphResponse(
        repo_sha=row.repo_sha,
        nodes=nodes,
        edges=[ImportEdgeResponse.model_validate(edge) for edge in edges],
        resolved_edges=sum(1 for edge in edges if edge.resolved),
        total_edges=len(edges),
    )


issues_router = APIRouter(prefix="/issues", tags=["issues"])


@issues_router.post(
    "/lookup",
    response_model=IssueLookupResponse,
    summary="Fetch a GitHub issue",
    description=(
        "Optional convenience. A token raises the rate limit and allows private "
        "repositories, but pasting the issue text into the run form always works "
        "and needs no credentials at all."
    ),
)
def lookup(payload: IssueLookupRequest) -> IssueLookupResponse:
    settings = get_settings()
    issue = fetch_github_issue(payload.repository_url, payload.issue_number, settings)
    return IssueLookupResponse(
        issue=issue, source="github", token_configured=bool(settings.github_token)
    )


models_router = APIRouter(prefix="/models", tags=["models"])


@models_router.get(
    "",
    response_model=list[ModelResponse],
    summary="Selectable models",
    description=(
        "Includes models that are not configured, with the reason, so the UI can "
        "explain why an option is disabled instead of hiding it."
    ),
)
def list_models() -> list[ModelResponse]:
    return [ModelResponse(**asdict(info)) for info in describe_models(get_settings())]
