"""The graph state and the observer interface used to stream it out."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from patchpilot_core.enums import RunState, RunStatus, StopReason
from patchpilot_core.models import (
    AttemptRecord,
    ContextPackage,
    IndexStats,
    IssueSpec,
    LatencyBreakdown,
    PatchProposal,
    PatchValidationResult,
    RepairPlan,
    RepositorySpec,
    RunConfig,
    SandboxExecution,
    StateTransition,
    TokenUsage,
)


class AgentState(TypedDict, total=False):
    """State carried through the LangGraph repair machine.

    Every field the specification requires to be inspectable lives here and is
    persisted after each transition, which is what makes a run resumable and
    auditable rather than a black box that either works or does not.
    """

    run_id: str
    model: str
    repository: RepositorySpec
    issue: IssueSpec
    config: RunConfig

    repo_path: str
    repo_sha: str
    index_stats: IndexStats | None
    commands: dict[str, Any]
    sandbox_backend: str
    sandbox_isolated: bool

    context: ContextPackage | None
    plan: RepairPlan | None
    plan_error: str | None
    patch: PatchProposal | None
    validation: PatchValidationResult | None
    execution: SandboxExecution | None
    baseline: SandboxExecution | None
    baseline_reproduced: bool | None

    attempt: int
    attempts: list[AttemptRecord]
    patch_hashes: list[str]
    failure_excerpt: str | None
    analysis: str
    should_retry: bool

    usage: TokenUsage
    latency: LatencyBreakdown
    transitions: list[StateTransition]

    state: RunState
    status: RunStatus
    stop_reason: StopReason | None
    error: str | None


@dataclass(slots=True)
class RunObserver:
    """Callbacks the API uses to persist and stream a run as it happens.

    All callbacks are optional and are never allowed to break a run: the graph
    wraps each call so a failing observer degrades to a log line.
    """

    on_transition: Callable[[StateTransition], None] | None = None
    on_attempt: Callable[[AttemptRecord], None] | None = None
    on_state: Callable[[AgentState], None] | None = None
    should_cancel: Callable[[], bool] | None = None

    def cancelled(self) -> bool:
        if self.should_cancel is None:
            return False
        try:
            return bool(self.should_cancel())
        except Exception:
            return False


def initial_state(
    *,
    run_id: str,
    repository: RepositorySpec,
    issue: IssueSpec,
    config: RunConfig,
) -> AgentState:
    return AgentState(
        run_id=run_id,
        model=config.model,
        repository=repository,
        issue=issue,
        config=config,
        repo_path="",
        repo_sha=repository.commit_sha or "",
        index_stats=None,
        commands={},
        sandbox_backend="",
        sandbox_isolated=False,
        context=None,
        plan=None,
        plan_error=None,
        patch=None,
        validation=None,
        execution=None,
        baseline=None,
        baseline_reproduced=None,
        attempt=0,
        attempts=[],
        patch_hashes=[],
        failure_excerpt=None,
        analysis="",
        should_retry=False,
        usage=TokenUsage(),
        latency=LatencyBreakdown(),
        transitions=[],
        # ``state`` is intentionally unset: the first recorded transition then reads
        # as "None -> INGEST" rather than the confusing "INGEST -> INGEST".
        status=RunStatus.QUEUED,
        stop_reason=None,
        error=None,
    )
