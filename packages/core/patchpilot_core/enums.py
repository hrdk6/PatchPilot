"""Enumerations shared across the agent, API, persistence and evaluation layers."""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    """Nodes of the LangGraph repair state machine."""

    INGEST = "INGEST"
    INDEX = "INDEX"
    RETRIEVE = "RETRIEVE"
    PLAN = "PLAN"
    GENERATE_PATCH = "GENERATE_PATCH"
    VALIDATE_PATCH = "VALIDATE_PATCH"
    SANDBOX_TEST = "SANDBOX_TEST"
    ANALYZE_RESULT = "ANALYZE_RESULT"
    REPAIR_OR_FINISH = "REPAIR_OR_FINISH"
    FINISHED = "FINISHED"


class RunStatus(StrEnum):
    """Lifecycle status of a run. Everything except the first two is terminal."""

    QUEUED = "queued"
    RUNNING = "running"

    FIXED = "fixed"
    """Patch applied and the validation command passed in the sandbox."""

    TESTS_FAILED = "tests-failed"
    """A syntactically valid patch applied but tests still fail."""

    PATCH_INVALID = "patch-invalid"
    """No proposal ever survived patch validation (safety or apply failure)."""

    SANDBOX_FAILED = "sandbox-failed"
    """The sandbox itself was unavailable or could not run the commands."""

    BUDGET_EXHAUSTED = "budget-exhausted"
    """The retry budget ran out before the validation command passed."""

    CANCELLED = "cancelled"

    ERROR = "error"
    """Unrecoverable infrastructure/setup failure outside the sandbox
    (clone failure, indexing failure, model adapter failure). Documented as a
    seventh terminal state in docs/architecture.md."""

    @property
    def is_terminal(self) -> bool:
        return self not in (RunStatus.QUEUED, RunStatus.RUNNING)


TERMINAL_STATUSES = frozenset(s for s in RunStatus if s.is_terminal)


class SandboxStatus(StrEnum):
    """Outcome of one sandbox execution, distinct from the exit code."""

    COMPLETED = "completed"
    """The command ran to completion; consult ``exit_code`` for pass/fail."""

    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output-limit"
    SETUP_FAILED = "setup-failed"
    UNAVAILABLE = "unavailable"
    """No sandbox backend could be started at all."""

    INTERNAL_ERROR = "internal-error"


class CommandKind(StrEnum):
    SETUP = "setup"
    BASELINE = "baseline"
    LINT = "lint"
    TYPECHECK = "typecheck"
    TEST = "test"
    VALIDATION = "validation"


class PatchRejectionReason(StrEnum):
    EMPTY = "empty-diff"
    MALFORMED = "malformed-diff"
    APPLY_FAILED = "apply-failed"
    PATH_TRAVERSAL = "path-traversal"
    PROTECTED_PATH = "protected-path"
    HIDDEN_FILE = "hidden-file"
    NEW_FILE_OUTSIDE_REPO = "new-file-outside-repo"
    TOO_MANY_FILES = "too-many-files"
    TOO_MANY_LINES = "too-many-lines"
    BINARY_PATCH = "binary-patch"
    NO_CHANGE = "no-effective-change"
    DUPLICATE = "duplicate-patch"


class StopReason(StrEnum):
    """Why the state machine stopped iterating."""

    VALIDATION_PASSED = "validation-passed"
    BUDGET_EXHAUSTED = "budget-exhausted"
    REPEATED_PATCH = "repeated-equivalent-patch"
    UNSAFE_PATCH = "unsafe-patch"
    SANDBOX_UNAVAILABLE = "sandbox-unavailable"
    SETUP_FAILED = "unrecoverable-setup-failure"
    CANCELLED = "cancelled"
    MODEL_ERROR = "model-error"
    ANALYSIS_GAVE_UP = "analysis-declined-retry"


class JobType(StrEnum):
    INDEX_REPOSITORY = "index_repository"
    AGENT_RUN = "agent_run"
    BENCHMARK_RUN = "benchmark_run"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SymbolType(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    IMPORT = "import"


class RetrievalReason(StrEnum):
    """Why a chunk made it into the context package — surfaced verbatim in the UI."""

    SEMANTIC = "semantic-similarity"
    LEXICAL = "lexical-overlap"
    IMPORT_NEIGHBOR = "import-graph-neighbor"
    CALL_NEIGHBOR = "call-graph-neighbor"
    TEST_FOR_SYMBOL = "test-referencing-symbol"
    FAILURE_TRACE = "named-in-failure-output"
    ISSUE_PATH_MENTION = "path-named-in-issue"
    CONVENTION_FILE = "repository-convention-file"
