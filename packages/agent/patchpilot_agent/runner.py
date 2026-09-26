"""The entry point everything else calls: run one repair attempt end to end."""

from __future__ import annotations

import shutil
from collections.abc import MutableMapping
from dataclasses import dataclass

from patchpilot_core.config import Settings, get_settings
from patchpilot_core.ids import run_id as new_run_id
from patchpilot_core.models import IssueSpec, RepositorySpec, RunConfig, RunSummary
from patchpilot_indexer import RepositoryIndex, RepositoryIndexer

from .adapters.factory import build_adapter
from .graph import RepairGraph, summarize
from .state import AgentState, RunObserver


@dataclass(slots=True)
class RunOutcome:
    state: AgentState
    summary: RunSummary


def run_agent(
    *,
    repository: RepositorySpec,
    issue: IssueSpec,
    config: RunConfig | None = None,
    settings: Settings | None = None,
    run_id: str | None = None,
    observer: RunObserver | None = None,
    indexer: RepositoryIndexer | None = None,
    index_cache: MutableMapping[str, RepositoryIndex] | None = None,
    transition_offset: int = 0,
) -> RunOutcome:
    """Execute the full repair state machine for one repository/issue pair.

    ``transition_offset`` continues the transition log of a run that was
    interrupted, so resuming appends to the recorded history instead of
    overwriting it.
    """
    settings = settings or get_settings()
    settings.ensure_dirs()
    config = config or RunConfig(
        model=settings.default_model,
        max_repair_attempts=settings.max_repair_attempts,
        retrieval_top_k=settings.retrieval_top_k,
        max_patch_files=settings.max_patch_files,
        max_patch_lines=settings.max_patch_lines,
    )

    graph = RepairGraph(
        adapter_factory=lambda model: build_adapter(model, settings),
        settings=settings,
        observer=observer,
        indexer=indexer,
        index_cache=index_cache,
        transition_offset=transition_offset,
    )
    identifier = run_id or new_run_id()
    try:
        state = graph.run(
            run_id=identifier,
            repository=repository,
            issue=issue,
            config=config,
        )
    finally:
        # Each attempt disposes of its own workspace; this removes the run's
        # directory that held them, and whatever an interrupted attempt left.
        shutil.rmtree(settings.workspace_root / identifier, ignore_errors=True)
    return RunOutcome(state=state, summary=summarize(state))
