"""SQLAlchemy models.

Rows are projections of the pydantic domain models in ``patchpilot_core``: the
columns that are queried or filtered are real columns, and the rest of the
document is stored as JSON. That keeps the schema small enough to migrate
confidently while still persisting every field the UI needs.

Everything here works unchanged on SQLite and PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    branch: Mapped[str | None] = mapped_column(String(200))
    commit_sha: Mapped[str | None] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(32), default="git-clone")
    local_path: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    indexes: Mapped[list[RepositoryIndexRow]] = relationship(
        back_populates="repository", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_repositories_url", "url"),)


class RepositoryIndexRow(Base):
    __tablename__ = "repository_indexes"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    repository_id: Mapped[str] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    repo_sha: Mapped[str] = mapped_column(String(80), nullable=False)
    root_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    conventions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    embedding_provider: Mapped[str] = mapped_column(String(64), default="hash")
    vector_store: Mapped[str] = mapped_column(String(64), default="local")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    repository: Mapped[Repository] = relationship(back_populates="indexes")

    __table_args__ = (
        UniqueConstraint("repository_id", "repo_sha", name="uq_index_repo_sha"),
        Index("ix_repository_indexes_sha", "repo_sha"),
    )


class CodeChunkRow(Base):
    """Symbol-aligned chunk metadata. The AST/graph facts the UI explains with."""

    __tablename__ = "code_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    index_id: Mapped[str] = mapped_column(
        ForeignKey("repository_indexes.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[str] = mapped_column(String(64), nullable=False)
    repo_sha: Mapped[str] = mapped_column(String(80), nullable=False)
    path: Mapped[str] = mapped_column(String(600), nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(400), nullable=False)
    symbol_type: Mapped[str] = mapped_column(String(32), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    docstring: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    imports: Mapped[list[Any]] = mapped_column(JSON, default=list)
    calls: Mapped[list[Any]] = mapped_column(JSON, default=list)
    calls_resolved: Mapped[list[Any]] = mapped_column(JSON, default=list)
    called_by: Mapped[list[Any]] = mapped_column(JSON, default=list)

    __table_args__ = (
        Index("ix_code_chunks_index_path", "index_id", "path"),
        UniqueConstraint("index_id", "chunk_id", name="uq_chunk_per_index"),
    )


class ImportEdgeRow(Base):
    __tablename__ = "import_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    index_id: Mapped[str] = mapped_column(
        ForeignKey("repository_indexes.id", ondelete="CASCADE"), nullable=False
    )
    source_path: Mapped[str] = mapped_column(String(600), nullable=False)
    target_path: Mapped[str | None] = mapped_column(String(600))
    module: Mapped[str] = mapped_column(String(400), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(200))
    line: Mapped[int] = mapped_column(Integer, default=0)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (Index("ix_import_edges_index_source", "index_id", "source_path"),)


class SymbolRow(Base):
    __tablename__ = "symbols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    index_id: Mapped[str] = mapped_column(
        ForeignKey("repository_indexes.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(String(600), nullable=False)
    qualified_name: Mapped[str] = mapped_column(String(400), nullable=False)
    symbol_type: Mapped[str] = mapped_column(String(32), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (Index("ix_symbols_index_path", "index_id", "path"),)


class Issue(Base):
    __tablename__ = "issues"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    repository_id: Mapped[str] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(16), default="manual")
    number: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(600), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(String(600))
    labels: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    repository_id: Mapped[str] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    issue_id: Mapped[str] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"), nullable=False
    )
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="INGEST", nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(48))
    repo_sha: Mapped[str | None] = mapped_column(String(80))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempts_used: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cost_known: Mapped[bool] = mapped_column(Boolean, default=True)
    latency: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    baseline: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    baseline_reproduced: Mapped[bool | None] = mapped_column(Boolean)
    context_package: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    commands: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sandbox_backend: Mapped[str | None] = mapped_column(String(32))
    sandbox_isolated: Mapped[bool] = mapped_column(Boolean, default=False)
    final_patch: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    benchmark_result_id: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list[RunEvent]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunEvent.index"
    )
    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="Attempt.attempt"
    )
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    # Read-only joins so a run summary can say what it was about without a
    # second query per row.
    issue: Mapped[Issue] = relationship(lazy="joined", viewonly=True)
    repository: Mapped[Repository] = relationship(lazy="joined", viewonly=True)

    @property
    def issue_title(self) -> str:
        return self.issue.title if self.issue is not None else ""

    @property
    def repository_slug(self) -> str:
        return self.repository.slug if self.repository is not None else ""

    __table_args__ = (
        Index("ix_runs_status_created", "status", "created_at"),
        Index("ix_runs_model", "model"),
    )


class RunEvent(Base):
    """One state-machine transition. Append-only."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint("run_id", "index", name="uq_event_index_per_run"),
        Index("ix_run_events_run", "run_id", "index"),
    )


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    plan_error: Mapped[str | None] = mapped_column(Text)
    diff: Mapped[str | None] = mapped_column(Text)
    diff_hash: Mapped[str | None] = mapped_column(String(32))
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    execution: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    analysis: Mapped[str] = mapped_column(Text, default="")
    should_retry: Mapped[bool] = mapped_column(Boolean, default=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="attempts")

    __table_args__ = (UniqueConstraint("run_id", "attempt", name="uq_attempt_per_run"),)


class Artifact(Base):
    """A file preserved after the sandbox was destroyed: a diff or a log."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    media_type: Mapped[str] = mapped_column(String(80), default="text/plain")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="artifacts")

    __table_args__ = (Index("ix_artifacts_run", "run_id", "attempt"),)


class Job(Base):
    """Database-backed work queue consumed by the in-process worker."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    correlation_id: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_jobs_status_created", "status", "created_at"),)


class BenchmarkRun(Base):
    __tablename__ = "benchmark_runs"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(200), nullable=False)
    models: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    markdown: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    results: Mapped[list[BenchmarkResult]] = relationship(
        back_populates="benchmark", cascade="all, delete-orphan"
    )


class BenchmarkResult(Base):
    __tablename__ = "benchmark_results"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    benchmark_id: Mapped[str] = mapped_column(
        ForeignKey("benchmark_runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    baseline_failed: Mapped[bool | None] = mapped_column(Boolean)
    patch_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts_used: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    sandbox_failed: Mapped[bool] = mapped_column(Boolean, default=False)
    similarity: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    benchmark: Mapped[BenchmarkRun] = relationship(back_populates="results")

    __table_args__ = (Index("ix_benchmark_results_benchmark", "benchmark_id", "model"),)
