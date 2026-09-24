"""HTTP request and response schemas.

These are the public contract and they appear in the OpenAPI document. They are
deliberately separate from the ORM rows and from the internal domain models:
renaming a column must not silently change the API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from patchpilot_core.models import (
    ContextPackage,
    IssueSpec,
    RepositorySpec,
    RunConfig,
    SandboxLimits,
)
from pydantic import BaseModel, Field, model_validator


class ApiModel(BaseModel):
    model_config = {"from_attributes": True}


class ErrorResponse(ApiModel):
    code: str
    message: str
    remediation: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None


# --------------------------------------------------------------------------- #
# Repositories and issues
# --------------------------------------------------------------------------- #
class RepositoryCreate(ApiModel):
    url: str = Field(description="HTTPS URL, ssh remote, file:// URL or local path")
    branch: str | None = None
    commit_sha: str | None = None
    name: str | None = None

    def to_spec(self) -> RepositorySpec:
        return RepositorySpec(
            url=self.url, branch=self.branch, commit_sha=self.commit_sha, name=self.name
        )


class RepositoryResponse(ApiModel):
    id: str
    url: str
    slug: str
    branch: str | None
    commit_sha: str | None
    source: str
    local_path: str | None
    created_at: datetime


class IssueResponse(ApiModel):
    id: str
    repository_id: str
    source: str
    number: int | None
    title: str
    body: str
    url: str | None
    labels: list[str]
    created_at: datetime


# --------------------------------------------------------------------------- #
# Indexing
# --------------------------------------------------------------------------- #
class IndexResponse(ApiModel):
    id: str
    repository_id: str
    repo_sha: str
    root_path: str
    stats: dict[str, Any]
    embedding_provider: str
    vector_store: str
    created_at: datetime


class SymbolResponse(ApiModel):
    path: str
    qualified_name: str
    symbol_type: str
    start_line: int
    end_line: int


class ImportEdgeResponse(ApiModel):
    source_path: str
    target_path: str | None
    module: str
    symbol: str | None
    line: int
    resolved: bool


class DependencyGraphResponse(ApiModel):
    repo_sha: str
    nodes: list[str]
    edges: list[ImportEdgeResponse]
    resolved_edges: int
    total_edges: int


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
class RunCreate(ApiModel):
    repository_url: str
    branch: str | None = None
    commit_sha: str | None = None
    repository_name: str | None = None

    issue_number: int | None = None
    issue_title: str | None = None
    issue_text: str | None = Field(
        default=None,
        description="Paste the issue body here to run without any GitHub access.",
    )

    model: str = "mock:deterministic"
    max_repair_attempts: int = Field(default=3, ge=1, le=10)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    retrieval_top_k: int = Field(default=12, ge=1, le=60)

    setup_command: str | None = None
    baseline_command: str | None = None
    validation_command: str | None = None
    lint_command: str | None = None
    typecheck_command: str | None = None
    reproduction_command: str | None = None

    sandbox_backend: Literal["auto", "docker", "local"] = "auto"
    sandbox_image: str | None = None
    timeout_seconds: int = Field(default=180, ge=10, le=3600)
    memory_mb: int = Field(default=1024, ge=128, le=16384)
    cpus: float = Field(default=1.0, gt=0, le=16)
    network: Literal["none", "bridge"] = "none"

    max_patch_files: int = Field(default=10, ge=1, le=100)
    max_patch_lines: int = Field(default=400, ge=1, le=5000)
    allowed_write_globs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _needs_an_issue(self) -> RunCreate:
        if self.issue_number is None and not (self.issue_text or "").strip():
            raise ValueError("provide issue_text, or issue_number for a GitHub repository")
        return self

    def to_repository_spec(self) -> RepositorySpec:
        return RepositorySpec(
            url=self.repository_url,
            branch=self.branch,
            commit_sha=self.commit_sha,
            name=self.repository_name,
        )

    def to_run_config(self) -> RunConfig:
        return RunConfig(
            model=self.model,
            temperature=self.temperature,
            max_repair_attempts=self.max_repair_attempts,
            setup_command=self.setup_command,
            baseline_command=self.baseline_command,
            validation_command=self.validation_command,
            lint_command=self.lint_command,
            typecheck_command=self.typecheck_command,
            reproduction_command=self.reproduction_command,
            retrieval_top_k=self.retrieval_top_k,
            max_patch_files=self.max_patch_files,
            max_patch_lines=self.max_patch_lines,
            sandbox_backend=self.sandbox_backend,
            sandbox_image=self.sandbox_image,
            allowed_write_globs=self.allowed_write_globs,
            limits=SandboxLimits(
                cpus=self.cpus,
                memory_mb=self.memory_mb,
                timeout_seconds=self.timeout_seconds,
                network=self.network,
            ),
        )


class RunSummaryResponse(ApiModel):
    id: str
    repository_id: str
    issue_id: str
    issue_title: str = ""
    repository_slug: str = ""
    model: str
    status: str
    state: str
    stop_reason: str | None
    repo_sha: str | None
    attempts_used: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cost_known: bool
    sandbox_backend: str | None
    sandbox_isolated: bool
    baseline_reproduced: bool | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunListResponse(ApiModel):
    items: list[RunSummaryResponse]
    total: int
    limit: int
    offset: int


class RunEventResponse(ApiModel):
    index: int
    from_state: str | None
    to_state: str
    reason: str
    attempt: int
    duration_ms: int
    detail: dict[str, Any]
    at: datetime


class AttemptResponse(ApiModel):
    attempt: int
    plan: dict[str, Any] | None
    plan_error: str | None
    diff: str | None
    diff_hash: str | None
    validation: dict[str, Any] | None
    execution: dict[str, Any] | None
    analysis: str
    should_retry: bool
    succeeded: bool
    input_tokens: int
    output_tokens: int
    duration_ms: int


class ArtifactResponse(ApiModel):
    id: str
    run_id: str
    attempt: int
    kind: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


class RetrievedChunkResponse(ApiModel):
    rank: int
    path: str
    symbol: str
    symbol_type: str
    start_line: int
    end_line: int
    score: float
    reasons: list[dict[str, Any]]
    is_test: bool
    content: str


class RunDetailResponse(ApiModel):
    run: RunSummaryResponse
    repository: RepositoryResponse
    issue: IssueResponse
    config: dict[str, Any]
    commands: dict[str, Any]
    latency: dict[str, Any]
    baseline: dict[str, Any] | None
    final_patch: str | None
    events: list[RunEventResponse]
    attempts: list[AttemptResponse]
    artifacts: list[ArtifactResponse]
    retrieval: list[RetrievedChunkResponse]
    retrieval_truncated: bool
    conventions: dict[str, str]


def retrieval_from_context(
    package: dict[str, Any] | None,
) -> tuple[list[RetrievedChunkResponse], bool, dict[str, str]]:
    if not package:
        return [], False, {}
    parsed = ContextPackage.model_validate(package)
    rows = [
        RetrievedChunkResponse(
            rank=item.rank,
            path=item.chunk.path,
            symbol=item.chunk.symbol,
            symbol_type=str(item.chunk.symbol_type),
            start_line=item.chunk.start_line,
            end_line=item.chunk.end_line,
            score=item.score,
            reasons=[component.model_dump(mode="json") for component in item.components],
            is_test=item.chunk.is_test,
            content=item.chunk.content,
        )
        for item in parsed.chunks
    ]
    return rows, parsed.truncated, parsed.conventions


# --------------------------------------------------------------------------- #
# Models, system
# --------------------------------------------------------------------------- #
class ModelResponse(ApiModel):
    id: str
    provider: str
    model: str
    available: bool
    reason: str
    is_test_double: bool
    pricing_known: bool
    input_per_mtok: float | None
    output_per_mtok: float | None


class SandboxStatusResponse(ApiModel):
    backend: str
    available: bool
    isolated: bool
    reason: str
    controls: dict[str, Any]


class SystemInfoResponse(ApiModel):
    version: str
    environment: str
    database: str
    vector_store: str
    embedding_provider: str
    default_model: str
    max_repair_attempts: int
    sandbox: SandboxStatusResponse
    worker_running: bool
    queued_jobs: int


class JobResponse(ApiModel):
    id: str
    type: str
    status: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


# --------------------------------------------------------------------------- #
# Benchmarks
# --------------------------------------------------------------------------- #
class DatasetTaskResponse(ApiModel):
    id: str
    repository_url: str
    commit_sha: str | None
    title: str
    difficulty: str
    tags: list[str]
    validation_command: str
    baseline_command: str | None
    expected_files: list[str]


class DatasetResponse(ApiModel):
    name: str
    path: str
    description: str
    task_count: int
    tags: list[str]
    tasks: list[DatasetTaskResponse]


class BenchmarkCreate(ApiModel):
    dataset: str = Field(description="Dataset name or path under fixtures/datasets")
    models: list[str] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class BenchmarkResultResponse(ApiModel):
    task_id: str
    model: str
    run_id: str | None
    status: str
    passed: bool
    baseline_failed: bool | None
    patch_applied: bool
    attempts_used: int
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    sandbox_failed: bool
    similarity: float | None
    tags: list[str]
    detail: dict[str, Any]


class BenchmarkResponse(ApiModel):
    id: str
    dataset: str
    models: list[str]
    tags: list[str]
    status: str
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    report: dict[str, Any] | None
    results: list[BenchmarkResultResponse] = Field(default_factory=list)


class IssueLookupRequest(ApiModel):
    repository_url: str
    issue_number: int


class IssueLookupResponse(ApiModel):
    issue: IssueSpec
    source: str
    token_configured: bool
