"""Typed domain models used at every system boundary.

These pydantic models are the contract between the indexer, the agent, the
sandbox, the API and the evaluation harness. Persistence rows are projections of
these; HTTP schemas are re-exports of these. There is exactly one definition of
"what a retrieved chunk is" in this codebase.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import (
    CommandKind,
    PatchRejectionReason,
    RetrievalReason,
    RunState,
    RunStatus,
    SandboxStatus,
    StopReason,
    SymbolType,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- #
# Repository + issue
# --------------------------------------------------------------------------- #
class RepositorySpec(Base):
    """Where the code comes from. ``commit_sha`` is resolved at ingest and pinned."""

    url: str
    branch: str | None = None
    commit_sha: str | None = None
    name: str | None = None

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("repository url is required")
        if "://" in value and not value.startswith(("http://", "https://", "file://")):
            scheme = value.split("://", 1)[0]
            raise ValueError(
                f"unsupported repository scheme {scheme!r}; use http(s), file://, an "
                "ssh remote (git@host:owner/repo) or a local path"
            )
        return value

    @property
    def slug(self) -> str:
        """A short, filesystem-safe name for this repository.

        Both separators are handled: a Windows path such as ``C:\\repos\\thing``
        has no forward slashes, and splitting on ``/`` alone would yield the whole
        path as the "name" -- which then behaves as an absolute path when joined
        onto a cache directory and silently escapes it.
        """
        raw = self.name or self.url
        tail = raw.rstrip("/\\").replace("\\", "/").split("/")[-1]
        cleaned = "".join(
            character if character.isalnum() or character in "-_." else "-"
            for character in tail.removesuffix(".git")
        ).strip("-.")
        return cleaned or "repository"


class IssueSpec(Base):
    """The bug report. Either fetched from GitHub or pasted by a human."""

    source: Literal["github", "manual"] = "manual"
    number: int | None = None
    title: str = ""
    body: str = ""
    url: str | None = None
    labels: list[str] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.body}".strip()

    def fingerprint(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


class CommandSpec(Base):
    """A shell command executed inside the sandbox."""

    kind: CommandKind
    command: str
    timeout_seconds: int | None = None
    allow_failure: bool = False

    @field_validator("command")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be empty")
        return value.strip()


class SandboxLimits(Base):
    cpus: float = 1.0
    memory_mb: int = 1024
    pids: int = 256
    timeout_seconds: int = 180
    max_output_bytes: int = 64_000
    network: Literal["none", "bridge"] = "none"
    read_only_rootfs: bool = True
    user: str = "10001:10001"


class RunConfig(Base):
    """Everything that makes a run reproducible, apart from the repo SHA and issue."""

    model: str = "mock:deterministic"
    temperature: float = 0.0
    max_repair_attempts: int = 3
    setup_command: str | None = None
    baseline_command: str | None = None
    validation_command: str | None = None
    lint_command: str | None = None
    typecheck_command: str | None = None
    reproduction_command: str | None = None
    retrieval_top_k: int = 12
    max_patch_files: int = 10
    max_patch_lines: int = 400
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    sandbox_backend: Literal["auto", "docker", "local"] = "auto"
    sandbox_image: str | None = None
    allowed_write_globs: list[str] = Field(default_factory=list)
    """Extra paths the patch validator should permit beyond the default policy."""


# --------------------------------------------------------------------------- #
# Indexing
# --------------------------------------------------------------------------- #
class SymbolRef(Base):
    """A named, addressable piece of code."""

    path: str
    qualified_name: str
    symbol_type: SymbolType
    start_line: int
    end_line: int

    @property
    def key(self) -> str:
        return f"{self.path}::{self.qualified_name}"


class CodeChunk(Base):
    """A symbol-aligned slice of the repository, the unit of retrieval."""

    chunk_id: str
    repo_sha: str
    path: str
    language: str
    symbol: str
    symbol_type: SymbolType
    start_line: int
    end_line: int
    content: str
    docstring: str | None = None
    imports: list[str] = Field(default_factory=list)
    calls: list[str] = Field(default_factory=list)
    """Raw call targets as written in the source, e.g. ``json.loads``.

    Kept unresolved so the dependency graph can be rebuilt idempotently from
    persisted chunks.
    """

    calls_resolved: list[str] = Field(default_factory=list)
    """Chunk keys this chunk calls, filled in by the dependency graph."""

    called_by: list[str] = Field(default_factory=list)
    """Chunk keys that call this chunk."""

    is_test: bool = False
    token_estimate: int = 0

    @property
    def key(self) -> str:
        return f"{self.path}::{self.symbol}"

    def header(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line} ({self.symbol_type}) {self.symbol}"


class ImportEdge(Base):
    source_path: str
    target_path: str | None
    module: str
    symbol: str | None = None
    line: int = 0
    resolved: bool = False


class FileRecord(Base):
    path: str
    language: str
    size_bytes: int
    sha256: str
    line_count: int
    is_test: bool = False
    parse_error: str | None = None


class IndexStats(Base):
    repo_sha: str
    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    skip_reasons: dict[str, int] = Field(default_factory=dict)
    symbols: int = 0
    chunks: int = 0
    import_edges: int = 0
    resolved_import_edges: int = 0
    parse_errors: int = 0
    duration_ms: int = 0
    embedded_chunks: int = 0


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #
class ScoreComponent(Base):
    reason: RetrievalReason
    weight: float
    detail: str = ""


class RetrievedChunk(Base):
    """A chunk plus a fully inspectable explanation of why it was retrieved."""

    chunk: CodeChunk
    score: float
    components: list[ScoreComponent] = Field(default_factory=list)
    rank: int = 0

    @property
    def reasons(self) -> list[RetrievalReason]:
        return [component.reason for component in self.components]

    def explain(self) -> str:
        parts = [
            f"{c.reason}(+{c.weight:.3f})" + (f" {c.detail}" if c.detail else "")
            for c in self.components
        ]
        return "; ".join(parts)


class ContextPackage(Base):
    """The compact, high-value context handed to the model."""

    query: str
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    conventions: dict[str, str] = Field(default_factory=dict)
    failure_excerpt: str | None = None
    repo_tree_excerpt: str = ""
    total_chars: int = 0
    truncated: bool = False

    def paths(self) -> list[str]:
        seen: list[str] = []
        for item in self.chunks:
            if item.chunk.path not in seen:
                seen.append(item.chunk.path)
        return seen


# --------------------------------------------------------------------------- #
# Planning + patching
# --------------------------------------------------------------------------- #
class RepairPlan(Base):
    """Structured plan the model must emit before it is allowed to write a patch."""

    root_cause: str
    files_to_change: list[str] = Field(default_factory=list)
    tests_to_run: list[str] = Field(default_factory=list)
    patch_strategy: str
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("root_cause", "patch_strategy")
    @classmethod
    def _needs_substance(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 10:
            raise ValueError("must be at least 10 characters of actual explanation")
        return cleaned


class FileChange(Base):
    path: str
    change_type: Literal["modify", "add", "delete", "rename"] = "modify"
    old_path: str | None = None
    lines_added: int = 0
    lines_removed: int = 0


class PatchProposal(Base):
    """A unified diff produced by the model for one attempt."""

    attempt: int
    diff: str
    summary: str = ""
    model: str = ""

    def normalized_hash(self) -> str:
        """Hash of the semantic content of the diff (context/whitespace insensitive).

        Used to detect an attempt equivalent to a previous one so the loop can stop
        instead of burning budget re-proposing the same idea.
        """
        meaningful: list[str] = []
        for line in self.diff.splitlines():
            if line.startswith(("+++", "---", "@@", "index ", "diff --git")):
                continue
            if line.startswith(("+", "-")):
                meaningful.append(line[0] + " ".join(line[1:].split()))
        return hashlib.sha256("\n".join(meaningful).encode("utf-8")).hexdigest()[:16]


class PatchValidationResult(Base):
    valid: bool
    rejections: list[PatchRejectionReason] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    changes: list[FileChange] = Field(default_factory=list)
    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0

    def reject(self, reason: PatchRejectionReason, message: str) -> None:
        self.valid = False
        if reason not in self.rejections:
            self.rejections.append(reason)
        self.messages.append(message)


# --------------------------------------------------------------------------- #
# Sandbox
# --------------------------------------------------------------------------- #
class CommandResult(Base):
    kind: CommandKind
    command: str
    exit_code: int | None
    status: SandboxStatus
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    truncated: bool = False
    bytes_dropped: int = 0

    @property
    def passed(self) -> bool:
        return self.status is SandboxStatus.COMPLETED and self.exit_code == 0

    def tail(self, limit: int = 4000) -> str:
        combined = (self.stdout + ("\n" + self.stderr if self.stderr else "")).strip()
        if len(combined) <= limit:
            return combined
        return "...[truncated]...\n" + combined[-limit:]


class SandboxExecution(Base):
    """The full result of running one attempt command set in one sandbox."""

    backend: str
    image: str | None = None
    status: SandboxStatus = SandboxStatus.COMPLETED
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    results: list[CommandResult] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utcnow)
    duration_ms: int = 0
    error: str | None = None
    workspace_digest: str | None = None

    def by_kind(self, kind: CommandKind) -> CommandResult | None:
        for result in self.results:
            if result.kind is kind:
                return result
        return None

    @property
    def ok(self) -> bool:
        return self.status is SandboxStatus.COMPLETED

    @property
    def validation_passed(self) -> bool:
        result = self.by_kind(CommandKind.VALIDATION) or self.by_kind(CommandKind.TEST)
        return bool(result and result.passed)

    def failure_excerpt(self, limit: int = 4000) -> str:
        for kind in (
            CommandKind.VALIDATION,
            CommandKind.TEST,
            CommandKind.TYPECHECK,
            CommandKind.LINT,
            CommandKind.SETUP,
        ):
            result = self.by_kind(kind)
            if result and not result.passed:
                return f"$ {result.command}\n(exit={result.exit_code})\n{result.tail(limit)}"
        return ""


# --------------------------------------------------------------------------- #
# Model usage + cost
# --------------------------------------------------------------------------- #
class TokenUsage(Base):
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    def __sub__(self, other: TokenUsage) -> TokenUsage:
        """The usage accrued since ``other`` was snapshotted. Never negative."""
        return TokenUsage(
            input_tokens=max(self.input_tokens - other.input_tokens, 0),
            output_tokens=max(self.output_tokens - other.output_tokens, 0),
        )


class ChatMessage(Base):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMResponse(Base):
    text: str
    model: str
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0
    finish_reason: str | None = None
    raw: dict[str, Any] | None = None


class CostEstimate(Base):
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    input_usd: float = 0.0
    output_usd: float = 0.0
    total_usd: float = 0.0
    pricing_known: bool = True
    note: str = ""


# --------------------------------------------------------------------------- #
# Run history
# --------------------------------------------------------------------------- #
class StateTransition(Base):
    index: int
    from_state: RunState | None
    to_state: RunState
    reason: str
    at: datetime = Field(default_factory=utcnow)
    duration_ms: int = 0
    attempt: int = 0
    detail: dict[str, Any] = Field(default_factory=dict)


class AttemptRecord(Base):
    """Everything produced by one pass through RETRIEVE to ANALYZE_RESULT."""

    attempt: int
    plan: RepairPlan | None = None
    plan_error: str | None = None
    patch: PatchProposal | None = None
    validation: PatchValidationResult | None = None
    execution: SandboxExecution | None = None
    analysis: str = ""
    should_retry: bool = False
    usage: TokenUsage = Field(default_factory=TokenUsage)
    """Tokens spent by this attempt alone; the run total is the sum over attempts."""

    started_at: datetime = Field(default_factory=utcnow)
    duration_ms: int = 0
    """Wall clock from this attempt's RETRIEVE to its ANALYZE_RESULT."""

    @property
    def succeeded(self) -> bool:
        return bool(self.execution and self.execution.validation_passed)


class LatencyBreakdown(Base):
    per_state_ms: dict[str, int] = Field(default_factory=dict)
    total_ms: int = 0

    def add(self, state: RunState | str, ms: int) -> None:
        key = str(state)
        self.per_state_ms[key] = self.per_state_ms.get(key, 0) + ms
        self.total_ms += ms


class RunSummary(Base):
    """The persisted, API-visible summary of a completed or in-flight run."""

    run_id: str
    status: RunStatus
    state: RunState
    repository: RepositorySpec
    issue: IssueSpec
    config: RunConfig
    repo_sha: str | None = None
    stop_reason: StopReason | None = None
    attempts_used: int = 0
    final_patch: PatchProposal | None = None
    baseline: SandboxExecution | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost: CostEstimate | None = None
    latency: LatencyBreakdown = Field(default_factory=LatencyBreakdown)
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    error: str | None = None
