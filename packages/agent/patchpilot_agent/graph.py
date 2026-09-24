"""The LangGraph repair state machine.

``INGEST -> INDEX -> RETRIEVE -> PLAN -> GENERATE_PATCH -> VALIDATE_PATCH ->
SANDBOX_TEST -> ANALYZE_RESULT -> REPAIR_OR_FINISH``, where REPAIR_OR_FINISH
either loops back to RETRIEVE with the failure in hand or terminates.

Design decisions worth stating:

* **ANALYZE_RESULT is deterministic.** It classifies the outcome with rules, not
  with another model call. Whether to spend another attempt is a budget decision;
  making it non-deterministic would make runs unreproducible and would let a
  model talk itself into an unbounded loop.
* **Every transition is recorded** with a reason, a duration and the attempt
  number, and is streamed to the observer before the next node starts. A run that
  crashes mid-flight still has an accurate history.
* **The retry budget is enforced in one place** (:meth:`_repair_or_finish`) and
  cannot be exceeded by any path through the graph.
* **Each attempt gets a brand-new workspace** copied from the pristine checkout,
  so attempt N+1 never inherits attempt N's edits.
"""

from __future__ import annotations

import textwrap
import time
from collections.abc import Callable, MutableMapping
from pathlib import Path

from langgraph.graph import END, StateGraph
from patchpilot_core.config import Settings, get_settings
from patchpilot_core.costs import estimate_cost
from patchpilot_core.diffutil import extract_diff_block
from patchpilot_core.enums import (
    CommandKind,
    PatchRejectionReason,
    RunState,
    RunStatus,
    SandboxStatus,
    StopReason,
)
from patchpilot_core.errors import (
    ModelAdapterError,
    PatchPilotError,
    StructuredOutputError,
)
from patchpilot_core.ids import attempt_id
from patchpilot_core.interfaces import LLMAdapter
from patchpilot_core.logging import get_logger, log_context
from patchpilot_core.models import (
    AttemptRecord,
    LatencyBreakdown,
    PatchProposal,
    RunSummary,
    SandboxExecution,
    StateTransition,
    TokenUsage,
    utcnow,
)
from patchpilot_core.textutil import summarize_failure
from patchpilot_indexer import HybridRetriever, RepositoryIndex, RepositoryIndexer
from patchpilot_sandbox import SandboxSelection, copy_snapshot, dispose, select_sandbox

from .commands import (
    DiscoveredCommands,
    build_command_plan,
    discover_commands,
    resolve_commands,
)
from .ingest import ingest_repository
from .patcher import PatchPolicy, PatchValidator, apply_patch
from .prompts import build_patch_messages, build_plan_messages
from .state import AgentState, RunObserver, initial_state
from .structured import parse_plan

logger = get_logger(__name__, component="agent")

# Rejections that mean the model tried to do something it must never do. These
# end the run immediately rather than spending another attempt.
UNSAFE_REJECTIONS = frozenset(
    {
        PatchRejectionReason.PATH_TRAVERSAL,
        PatchRejectionReason.PROTECTED_PATH,
        PatchRejectionReason.NEW_FILE_OUTSIDE_REPO,
        PatchRejectionReason.BINARY_PATCH,
    }
)


class RepairGraph:
    """Builds and runs the compiled LangGraph machine for one run."""

    def __init__(
        self,
        *,
        adapter_factory: Callable[[str], LLMAdapter],
        settings: Settings | None = None,
        observer: RunObserver | None = None,
        indexer: RepositoryIndexer | None = None,
        index_cache: MutableMapping[str, RepositoryIndex] | None = None,
        transition_offset: int = 0,
    ) -> None:
        self.settings = settings or get_settings()
        self.adapter_factory = adapter_factory
        self.observer = observer or RunObserver()
        self.indexer = indexer or RepositoryIndexer(self.settings)
        self.index_cache = index_cache if index_cache is not None else {}
        self._adapter: LLMAdapter | None = None
        self._retriever: HybridRetriever | None = None
        self._index: RepositoryIndex | None = None
        self._sandbox_selection: SandboxSelection | None = None
        # A resumed run continues its append-only transition log rather than
        # restarting the numbering and colliding with its own history.
        self._transition_index = transition_offset
        self.resumed = transition_offset > 0
        self._node_started = time.perf_counter()
        # Per-attempt accounting. Each attempt starts at RETRIEVE; the run-level
        # usage keeps accumulating, so an attempt's own share is the difference.
        self._attempt_clock = time.perf_counter()
        self._attempt_started_at = utcnow()
        self._attempt_usage_baseline = TokenUsage()
        # The last state a node emitted; what survives if a later node raises.
        self._latest_state: AgentState = {}
        self.graph = self._build()

    # ------------------------------------------------------------------ build
    def _build(self):
        builder: StateGraph = StateGraph(AgentState)
        builder.add_node(RunState.INGEST, self._ingest)
        builder.add_node(RunState.INDEX, self._index_repository)
        builder.add_node(RunState.RETRIEVE, self._retrieve)
        builder.add_node(RunState.PLAN, self._plan)
        builder.add_node(RunState.GENERATE_PATCH, self._generate_patch)
        builder.add_node(RunState.VALIDATE_PATCH, self._validate_patch)
        builder.add_node(RunState.SANDBOX_TEST, self._sandbox_test)
        builder.add_node(RunState.ANALYZE_RESULT, self._analyze_result)
        builder.add_node(RunState.REPAIR_OR_FINISH, self._repair_or_finish)

        builder.set_entry_point(RunState.INGEST)
        builder.add_conditional_edges(RunState.INGEST, self._after_terminal_check(RunState.INDEX))
        builder.add_conditional_edges(RunState.INDEX, self._after_terminal_check(RunState.RETRIEVE))
        builder.add_conditional_edges(RunState.RETRIEVE, self._after_terminal_check(RunState.PLAN))
        builder.add_conditional_edges(RunState.PLAN, self._after_plan)
        builder.add_conditional_edges(
            RunState.GENERATE_PATCH, self._after_terminal_check(RunState.VALIDATE_PATCH)
        )
        builder.add_edge(RunState.VALIDATE_PATCH, RunState.SANDBOX_TEST)
        builder.add_edge(RunState.SANDBOX_TEST, RunState.ANALYZE_RESULT)
        builder.add_edge(RunState.ANALYZE_RESULT, RunState.REPAIR_OR_FINISH)
        builder.add_conditional_edges(
            RunState.REPAIR_OR_FINISH,
            self._route_after_decision,
            {RunState.RETRIEVE: RunState.RETRIEVE, END: END},
        )
        return builder.compile()

    @staticmethod
    def _is_finished(state: AgentState) -> bool:
        """Any terminal status ends the graph, not just errors and cancellation.

        A node that terminates the run (an unavailable sandbox during INGEST, for
        example) must not be followed by INDEX and a wasted attempt.
        """
        status = state.get("status")
        return bool(status is not None and RunStatus(status).is_terminal)

    @classmethod
    def _after_terminal_check(cls, next_state: RunState):
        def route(state: AgentState) -> str:
            return END if cls._is_finished(state) else next_state

        return route

    @classmethod
    def _after_plan(cls, state: AgentState) -> str:
        if cls._is_finished(state):
            return END
        if state.get("plan") is None:
            # A malformed plan is an attempt outcome, not a crash: analyse it and
            # let the budget rules decide whether another attempt is justified.
            return RunState.ANALYZE_RESULT
        return RunState.GENERATE_PATCH

    @staticmethod
    def _route_after_decision(state: AgentState) -> str:
        return RunState.RETRIEVE if state.get("should_retry") else END

    # -------------------------------------------------------------- execution
    def run(
        self,
        *,
        run_id: str,
        repository,
        issue,
        config,
    ) -> AgentState:
        state = initial_state(run_id=run_id, repository=repository, issue=issue, config=config)
        state["status"] = RunStatus.RUNNING
        if self.resumed:
            logger.info(
                "resuming an interrupted run",
                extra={"run_id": run_id, "from_transition": self._transition_index},
            )
        with log_context(run_id=run_id, model=config.model):
            self._node_started = time.perf_counter()
            self._latest_state = state
            try:
                final: AgentState = self.graph.invoke(
                    state, config={"recursion_limit": 4 * config.max_repair_attempts + 24}
                )
            except PatchPilotError as exc:
                logger.exception("run failed", extra={"code": exc.code})
                return self._fail(self._latest_state, exc.message)
            except Exception as exc:
                logger.exception("run failed with an unexpected error")
                return self._fail(self._latest_state, f"{type(exc).__name__}: {exc}")
            self._emit_state(final)
            return final

    def _fail(self, state: AgentState, message: str) -> AgentState:
        """Close the run after an exception escaped a node.

        ``state`` is the most recent state a node emitted, so the transitions,
        attempts and token usage recorded before the failure survive into the
        summary, and the timeline ends in FINISHED with the error as its reason
        rather than stopping mid-flight with no explanation.
        """
        state["status"] = RunStatus.ERROR
        state["stop_reason"] = StopReason.SETUP_FAILED
        state["error"] = message
        state["should_retry"] = False
        self._record(
            state,
            RunState.FINISHED,
            reason=f"final status {RunStatus.ERROR}: {message}",
            detail={
                "terminal": True,
                "status": str(RunStatus.ERROR),
                "stop_reason": str(StopReason.SETUP_FAILED),
            },
        )
        return state

    # ------------------------------------------------------------------ nodes
    def _ingest(self, state: AgentState) -> AgentState:
        self._start_node()
        if self._check_cancelled(state):
            return state

        config = state["config"]
        ingested = ingest_repository(state["repository"], self.settings)
        state["repo_path"] = str(ingested.path)
        state["repo_sha"] = ingested.repo_sha
        state["repository"] = ingested.spec

        discovered = resolve_commands(config, discover_commands(ingested.path))
        state["commands"] = discovered.as_dict()

        selection = select_sandbox(
            self.settings, backend=config.sandbox_backend, image=config.sandbox_image
        )
        self._sandbox_selection = selection
        state["sandbox_backend"] = selection.backend
        state["sandbox_isolated"] = selection.isolated
        if not selection.available:
            return self._terminate(
                state,
                RunStatus.SANDBOX_FAILED,
                StopReason.SANDBOX_UNAVAILABLE,
                selection.reason,
                RunState.INGEST,
            )

        if not discovered.test:
            return self._terminate(
                state,
                RunStatus.ERROR,
                StopReason.SETUP_FAILED,
                "No test command was configured and none could be discovered. "
                "Supply a validation command for this repository.",
                RunState.INGEST,
            )

        baseline_specs = build_command_plan(discovered, config, phase="baseline")
        if baseline_specs:
            workspace = self._workspace_path(state, "baseline")
            copy_snapshot(ingested.path, workspace)
            try:
                baseline = selection.sandbox.run(workspace, baseline_specs, limits=config.limits)
            finally:
                dispose(workspace)
            state["baseline"] = baseline
            result = baseline.by_kind(CommandKind.BASELINE)
            if baseline.status is SandboxStatus.UNAVAILABLE:
                return self._terminate(
                    state,
                    RunStatus.SANDBOX_FAILED,
                    StopReason.SANDBOX_UNAVAILABLE,
                    baseline.error or "the sandbox is unavailable",
                    RunState.INGEST,
                )
            state["baseline_reproduced"] = bool(result and not result.passed)
            if result is not None:
                state["failure_excerpt"] = summarize_failure(result.tail(8000))

        baseline_note = (
            "reproduced the failure"
            if state.get("baseline_reproduced")
            else "did not fail (the bug may not reproduce)"
        )
        self._record(
            state,
            RunState.INGEST,
            reason=(
                f"pinned {state['repo_sha']}; sandbox={selection.backend}"
                f"{'' if selection.isolated else ' (NOT isolated)'}; "
                f"baseline {baseline_note}"
            ),
            detail={
                "repo_sha": state["repo_sha"],
                "commands": state["commands"],
                "sandbox": selection.describe(),
                "baseline_reproduced": state.get("baseline_reproduced"),
            },
        )
        return state

    def _index_repository(self, state: AgentState) -> AgentState:
        self._start_node()
        if self._check_cancelled(state):
            return state

        repo_sha = state["repo_sha"]
        cached = self.index_cache.get(repo_sha)
        if cached is not None and Path(cached.root) == Path(state["repo_path"]):
            index = cached
            reused = True
        else:
            index = self.indexer.index(Path(state["repo_path"]), repo_sha)
            self.index_cache[repo_sha] = index
            reused = False

        self._index = index
        self._retriever = HybridRetriever(
            index, self.indexer.embedder, self.indexer.vector_store, self.settings
        )
        state["index_stats"] = index.stats
        self._record(
            state,
            RunState.INDEX,
            reason=(
                f"{'reused cached index' if reused else 'indexed'} "
                f"{index.stats.files_indexed} files into {index.stats.chunks} "
                f"symbol chunks ({index.stats.resolved_import_edges}/"
                f"{index.stats.import_edges} imports resolved)"
            ),
            detail=index.stats.model_dump(mode="json"),
        )
        return state

    def _retrieve(self, state: AgentState) -> AgentState:
        self._start_node()
        if self._check_cancelled(state):
            return state
        assert self._retriever is not None

        self._attempt_clock = time.perf_counter()
        self._attempt_started_at = utcnow()
        self._attempt_usage_baseline = state.get("usage", TokenUsage())

        config = state["config"]
        package = self._retriever.retrieve(
            state["issue"].text,
            failure_output=state.get("failure_excerpt"),
            top_k=config.retrieval_top_k,
        )
        state["context"] = package
        self._record(
            state,
            RunState.RETRIEVE,
            reason=(
                f"selected {len(package.chunks)} chunks across "
                f"{len(package.paths())} files ({package.total_chars} chars"
                f"{', truncated' if package.truncated else ''})"
            ),
            detail={
                "paths": package.paths(),
                "chunks": [
                    {
                        "path": item.chunk.path,
                        "symbol": item.chunk.symbol,
                        "score": item.score,
                        "reasons": [str(reason) for reason in item.reasons],
                    }
                    for item in package.chunks
                ],
            },
        )
        return state

    def _plan(self, state: AgentState) -> AgentState:
        self._start_node()
        if self._check_cancelled(state):
            return state

        state["attempt"] = state.get("attempt", 0) + 1
        state["plan"] = None
        state["plan_error"] = None
        state["patch"] = None
        state["validation"] = None
        state["execution"] = None

        context = state.get("context")
        assert context is not None, "PLAN runs only after RETRIEVE has built a context"

        adapter = self._get_adapter(state)
        messages = build_plan_messages(
            state["issue"],
            context,
            attempt=state["attempt"],
            history=state.get("attempts", []),
        )
        try:
            response = adapter.complete(messages, temperature=state["config"].temperature)
        except ModelAdapterError as exc:
            return self._terminate(
                state, RunStatus.ERROR, StopReason.MODEL_ERROR, exc.message, RunState.PLAN
            )

        self._add_usage(state, response.usage)
        try:
            plan = parse_plan(response.text)
        except StructuredOutputError as exc:
            state["plan_error"] = exc.message
            self._record(
                state,
                RunState.PLAN,
                reason=f"the model returned an invalid plan: {exc.message}",
                detail={"context": exc.context, "raw_excerpt": response.text[:500]},
            )
            return state

        state["plan"] = plan
        self._record(
            state,
            RunState.PLAN,
            reason=(
                f"root cause: {textwrap.shorten(plan.root_cause, 120, placeholder='…')} "
                f"(confidence {plan.confidence:.2f})"
            ),
            detail=plan.model_dump(mode="json"),
        )
        return state

    def _generate_patch(self, state: AgentState) -> AgentState:
        self._start_node()
        if self._check_cancelled(state):
            return state

        context = state.get("context")
        plan = state.get("plan")
        assert context is not None and plan is not None, (
            "GENERATE_PATCH runs only after PLAN produced a validated plan"
        )

        adapter = self._get_adapter(state)
        messages = build_patch_messages(
            state["issue"],
            context,
            plan,
            attempt=state["attempt"],
            history=state.get("attempts", []),
        )
        try:
            response = adapter.complete(messages, temperature=state["config"].temperature)
        except ModelAdapterError as exc:
            return self._terminate(
                state,
                RunStatus.ERROR,
                StopReason.MODEL_ERROR,
                exc.message,
                RunState.GENERATE_PATCH,
            )

        self._add_usage(state, response.usage)
        raw = response.text.strip()
        diff = extract_diff_block(raw) or raw
        proposal = PatchProposal(
            attempt=state["attempt"],
            diff=diff,
            summary=plan.patch_strategy,
            model=state["model"],
        )
        state["patch"] = proposal
        self._record(
            state,
            RunState.GENERATE_PATCH,
            reason=f"model produced a {len(diff.splitlines())}-line diff",
            detail={"diff_hash": proposal.normalized_hash()},
        )
        return state

    def _validate_patch(self, state: AgentState) -> AgentState:
        self._start_node()
        config = state["config"]
        validator = PatchValidator(
            Path(state["repo_path"]),
            PatchPolicy(
                max_files=config.max_patch_files,
                max_lines=config.max_patch_lines,
                allow_hidden_files=self.settings.allow_patching_hidden_files,
                allowed_globs=tuple(config.allowed_write_globs),
            ),
        )
        proposal = state.get("patch")
        assert proposal is not None, "VALIDATE_PATCH runs only after GENERATE_PATCH"

        result = validator.validate(proposal, previous_hashes=set(state.get("patch_hashes", [])))
        state["validation"] = result
        hashes = list(state.get("patch_hashes", []))
        hashes.append(proposal.normalized_hash())
        state["patch_hashes"] = hashes

        self._record(
            state,
            RunState.VALIDATE_PATCH,
            reason=(
                f"accepted: {result.files_changed} file(s), "
                f"+{result.lines_added}/-{result.lines_removed}"
                if result.valid
                else "rejected: " + "; ".join(result.messages[:2])
            ),
            detail=result.model_dump(mode="json"),
        )
        return state

    def _sandbox_test(self, state: AgentState) -> AgentState:
        self._start_node()
        validation = state.get("validation")
        if validation is None or not validation.valid:
            return state
        if self._check_cancelled(state):
            return state

        config = state["config"]
        proposal = state.get("patch")
        selection = self._sandbox_selection
        assert proposal is not None and selection is not None, (
            "SANDBOX_TEST runs only after a proposal survived VALIDATE_PATCH"
        )

        workspace = self._workspace_path(state, f"attempt-{state['attempt']}")
        info = copy_snapshot(Path(state["repo_path"]), workspace)
        try:
            apply_patch(workspace, proposal)
            discovered = DiscoveredCommands(
                **{
                    key: value
                    for key, value in state["commands"].items()
                    if key in {"test", "lint", "typecheck", "setup", "provenance"}
                }
            )
            specs = build_command_plan(
                discovered, config, plan=state.get("plan"), phase="validation"
            )
            execution = selection.sandbox.run(workspace, specs, limits=config.limits)
            execution.workspace_digest = info.digest
        except Exception as exc:
            execution = SandboxExecution(
                backend=selection.backend,
                status=SandboxStatus.INTERNAL_ERROR,
                limits=config.limits,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            dispose(workspace)

        state["execution"] = execution
        passed = execution.validation_passed
        self._record(
            state,
            RunState.SANDBOX_TEST,
            reason=(
                f"{execution.backend}: "
                + (
                    "validation passed"
                    if passed
                    else f"status={execution.status}, "
                    + ", ".join(f"{result.kind}={result.exit_code}" for result in execution.results)
                )
            ),
            detail={
                "status": str(execution.status),
                "duration_ms": execution.duration_ms,
                "results": [
                    {
                        "kind": str(result.kind),
                        "command": result.command,
                        "exit_code": result.exit_code,
                        "duration_ms": result.duration_ms,
                        "truncated": result.truncated,
                    }
                    for result in execution.results
                ],
            },
        )
        return state

    def _analyze_result(self, state: AgentState) -> AgentState:
        """Classify the attempt and decide, by rule, whether a retry is justified."""
        self._start_node()
        config = state["config"]
        attempt = state["attempt"]
        budget_left = attempt < config.max_repair_attempts

        validation = state.get("validation")
        execution = state.get("execution")
        analysis: str
        should_retry = False
        stop_reason: StopReason | None = None
        status: RunStatus | None = None

        if state.get("plan") is None:
            analysis = (
                f"The model did not produce a usable plan ({state.get('plan_error')}). "
                "Nothing was executed."
            )
            should_retry = budget_left
            if not should_retry:
                status, stop_reason = RunStatus.PATCH_INVALID, StopReason.BUDGET_EXHAUSTED

        elif validation is not None and not validation.valid:
            unsafe = set(validation.rejections) & UNSAFE_REJECTIONS
            duplicate = PatchRejectionReason.DUPLICATE in validation.rejections
            analysis = "The patch was rejected before execution: " + "; ".join(
                validation.messages[:3]
            )
            if unsafe:
                should_retry = False
                status, stop_reason = RunStatus.PATCH_INVALID, StopReason.UNSAFE_PATCH
                analysis += " This is a policy violation, so the run stops here."
            elif duplicate:
                should_retry = False
                status, stop_reason = RunStatus.PATCH_INVALID, StopReason.REPEATED_PATCH
                analysis += " The model is repeating itself, so the run stops here."
            else:
                should_retry = budget_left
                if not should_retry:
                    status, stop_reason = (
                        RunStatus.PATCH_INVALID,
                        StopReason.BUDGET_EXHAUSTED,
                    )

        elif execution is None:
            analysis = "No sandbox execution was recorded for this attempt."
            status, stop_reason = RunStatus.ERROR, StopReason.SETUP_FAILED

        elif execution.status is SandboxStatus.UNAVAILABLE:
            analysis = f"The sandbox was unavailable: {execution.error}"
            status, stop_reason = RunStatus.SANDBOX_FAILED, StopReason.SANDBOX_UNAVAILABLE

        elif execution.status in (SandboxStatus.SETUP_FAILED, SandboxStatus.INTERNAL_ERROR):
            analysis = f"The sandbox could not run the commands: {execution.error}"
            status, stop_reason = RunStatus.SANDBOX_FAILED, StopReason.SETUP_FAILED

        elif execution.validation_passed:
            analysis = "The validation command passed inside the sandbox."
            status, stop_reason = RunStatus.FIXED, StopReason.VALIDATION_PASSED

        elif execution.status is SandboxStatus.TIMEOUT:
            analysis = (
                "A command exceeded the wall-clock limit. The patch may have "
                "introduced an infinite loop or a very slow path."
            )
            should_retry = budget_left
            if not should_retry:
                status, stop_reason = RunStatus.TESTS_FAILED, StopReason.BUDGET_EXHAUSTED

        else:
            failing = [
                f"{result.kind} exited {result.exit_code}"
                for result in execution.results
                if not result.passed
            ]
            analysis = (
                "The patch applied but the suite still fails (" + ", ".join(failing[:3]) + ")."
            )
            should_retry = budget_left
            if not should_retry:
                status, stop_reason = RunStatus.BUDGET_EXHAUSTED, StopReason.BUDGET_EXHAUSTED

        if execution is not None:
            excerpt = execution.failure_excerpt(6000)
            state["failure_excerpt"] = summarize_failure(excerpt) if excerpt else None
        elif validation is not None and not validation.valid:
            state["failure_excerpt"] = "Patch rejected: " + "; ".join(validation.messages[:3])

        state["analysis"] = analysis
        state["should_retry"] = should_retry
        if status is not None:
            state["status"] = status
        if stop_reason is not None:
            state["stop_reason"] = stop_reason

        record = AttemptRecord(
            attempt=attempt,
            plan=state.get("plan"),
            plan_error=state.get("plan_error"),
            patch=state.get("patch"),
            validation=validation,
            execution=execution,
            analysis=analysis,
            should_retry=should_retry,
            usage=state.get("usage", TokenUsage()) - self._attempt_usage_baseline,
            started_at=self._attempt_started_at,
            duration_ms=int((time.perf_counter() - self._attempt_clock) * 1000),
        )
        attempts = list(state.get("attempts", []))
        attempts.append(record)
        state["attempts"] = attempts
        self._notify_attempt(record)

        self._record(
            state,
            RunState.ANALYZE_RESULT,
            reason=analysis,
            detail={
                "should_retry": should_retry,
                "attempt": attempt,
                "budget": config.max_repair_attempts,
            },
        )
        return state

    def _repair_or_finish(self, state: AgentState) -> AgentState:
        """The single place where the retry budget is enforced."""
        self._start_node()
        config = state["config"]
        attempt = state["attempt"]

        if self.observer.cancelled():
            state["should_retry"] = False
            return self._terminate(
                state,
                RunStatus.CANCELLED,
                StopReason.CANCELLED,
                "the run was cancelled",
                RunState.REPAIR_OR_FINISH,
            )

        if state.get("should_retry") and attempt >= config.max_repair_attempts:
            # Belt and braces: ANALYZE_RESULT already checks the budget.
            state["should_retry"] = False
            state["status"] = RunStatus.BUDGET_EXHAUSTED
            state["stop_reason"] = StopReason.BUDGET_EXHAUSTED

        if state.get("should_retry"):
            self._record(
                state,
                RunState.REPAIR_OR_FINISH,
                reason=f"retrying: attempt {attempt + 1} of {config.max_repair_attempts}",
                detail={"attempt": attempt, "budget": config.max_repair_attempts},
            )
            return state

        if state.get("status") in (RunStatus.QUEUED, RunStatus.RUNNING):
            state["status"] = RunStatus.TESTS_FAILED
            state["stop_reason"] = state.get("stop_reason") or StopReason.ANALYSIS_GAVE_UP

        self._record(
            state,
            RunState.REPAIR_OR_FINISH,
            reason="no further attempt is justified",
            detail={"attempts_used": attempt, "budget": config.max_repair_attempts},
        )
        self._record(
            state,
            RunState.FINISHED,
            reason=f"final status {state['status']} ({state.get('stop_reason')})",
            detail={
                "attempts_used": attempt,
                "status": str(state["status"]),
                "stop_reason": str(state.get("stop_reason")),
            },
        )
        return state

    # ---------------------------------------------------------------- helpers
    def _get_adapter(self, state: AgentState) -> LLMAdapter:
        if self._adapter is None:
            self._adapter = self.adapter_factory(state["model"])
            bind = getattr(self._adapter, "bind_workspace", None)
            if callable(bind):
                bind(Path(state["repo_path"]), state["repo_sha"])
        return self._adapter

    def _workspace_path(self, state: AgentState, label: str) -> Path:
        root = self.settings.workspace_root / state["run_id"]
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{label}-{attempt_id()[-6:]}"

    def _add_usage(self, state: AgentState, usage: TokenUsage) -> None:
        state["usage"] = state.get("usage", TokenUsage()) + usage

    def _start_node(self) -> None:
        self._node_started = time.perf_counter()

    def _check_cancelled(self, state: AgentState) -> bool:
        if not self.observer.cancelled():
            return False
        self._terminate(
            state,
            RunStatus.CANCELLED,
            StopReason.CANCELLED,
            "the run was cancelled",
            state.get("state", RunState.INGEST),
        )
        return True

    def _terminate(
        self,
        state: AgentState,
        status: RunStatus,
        stop_reason: StopReason,
        message: str,
        at: RunState,
    ) -> AgentState:
        state["status"] = status
        state["stop_reason"] = stop_reason
        state["error"] = message
        state["should_retry"] = False
        self._record(state, at, reason=f"{status}: {message}", detail={"terminal": True})
        self._record(
            state,
            RunState.FINISHED,
            reason=f"final status {status} ({stop_reason})",
            detail={"status": str(status), "stop_reason": str(stop_reason)},
        )
        return state

    def _record(
        self,
        state: AgentState,
        to_state: RunState,
        *,
        reason: str,
        detail: dict | None = None,
    ) -> None:
        duration_ms = int((time.perf_counter() - self._node_started) * 1000)
        previous = state.get("state")
        transition = StateTransition(
            index=self._transition_index,
            from_state=previous,
            to_state=to_state,
            reason=reason,
            duration_ms=duration_ms,
            attempt=state.get("attempt", 0),
            detail=detail or {},
            at=utcnow(),
        )
        self._transition_index += 1
        state["state"] = to_state
        state.setdefault("transitions", []).append(transition)
        latency = state.get("latency")
        if latency is not None:
            latency.add(to_state, duration_ms)
        logger.info(
            "state transition",
            extra={
                "run_id": state.get("run_id"),
                "from_state": str(previous) if previous else None,
                "to_state": str(to_state),
                "attempt": state.get("attempt", 0),
                "duration_ms": duration_ms,
                "reason": reason,
            },
        )
        self._notify_transition(transition)
        self._emit_state(state)
        self._node_started = time.perf_counter()

    # ------------------------------------------------------------- observers
    def _notify_transition(self, transition: StateTransition) -> None:
        if self.observer.on_transition is None:
            return
        try:
            self.observer.on_transition(transition)
        except Exception:
            logger.warning("transition observer failed", exc_info=True)

    def _notify_attempt(self, record: AttemptRecord) -> None:
        if self.observer.on_attempt is None:
            return
        try:
            self.observer.on_attempt(record)
        except Exception:
            logger.warning("attempt observer failed", exc_info=True)

    def _emit_state(self, state: AgentState) -> None:
        self._latest_state = state
        if self.observer.on_state is None:
            return
        try:
            self.observer.on_state(state)
        except Exception:
            logger.warning("state observer failed", exc_info=True)


def summarize(state: AgentState) -> RunSummary:
    """Project the final graph state into the persisted, API-visible summary."""
    attempts = state.get("attempts", [])
    final_patch = None
    for record in reversed(attempts):
        if record.succeeded and record.patch is not None:
            final_patch = record.patch
            break
    if final_patch is None and attempts and attempts[-1].patch is not None:
        final_patch = attempts[-1].patch

    usage = state.get("usage", TokenUsage())
    return RunSummary(
        run_id=state["run_id"],
        status=state.get("status", RunStatus.ERROR),
        state=state.get("state", RunState.FINISHED),
        repository=state["repository"],
        issue=state["issue"],
        config=state["config"],
        repo_sha=state.get("repo_sha"),
        stop_reason=state.get("stop_reason"),
        attempts_used=state.get("attempt", 0),
        final_patch=final_patch,
        baseline=state.get("baseline"),
        usage=usage,
        cost=estimate_cost(state.get("model", ""), usage),
        latency=state.get("latency") or LatencyBreakdown(),
        finished_at=utcnow(),
        error=state.get("error"),
    )
