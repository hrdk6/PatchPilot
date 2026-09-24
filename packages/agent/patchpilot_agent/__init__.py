"""The PatchPilot agent: retrieval-grounded planning, patching and repair."""

from __future__ import annotations

from .adapters.anthropic import AnthropicAdapter
from .adapters.factory import ModelInfo, build_adapter, describe_models, split_model
from .adapters.mock import MOCK_VARIANTS, MockAdapter, load_solutions
from .adapters.openai_compat import OpenAICompatAdapter
from .commands import (
    DiscoveredCommands,
    build_command_plan,
    discover_commands,
    resolve_commands,
    targeted_test_command,
)
from .graph import RepairGraph, summarize
from .ingest import (
    IngestedRepository,
    content_digest,
    fetch_github_issue,
    ingest_repository,
    parse_github_repo,
    resolve_issue,
)
from .patcher import PROTECTED_GLOBS, PatchPolicy, PatchValidator, apply_patch
from .prompts import build_patch_messages, build_plan_messages, render_context
from .runner import RunOutcome, run_agent
from .state import AgentState, RunObserver, initial_state
from .structured import coerce_plan, extract_json_object, parse_plan

__all__ = [
    "MOCK_VARIANTS",
    "PROTECTED_GLOBS",
    "AgentState",
    "AnthropicAdapter",
    "DiscoveredCommands",
    "IngestedRepository",
    "MockAdapter",
    "ModelInfo",
    "OpenAICompatAdapter",
    "PatchPolicy",
    "PatchValidator",
    "RepairGraph",
    "RunObserver",
    "RunOutcome",
    "apply_patch",
    "build_adapter",
    "build_command_plan",
    "build_patch_messages",
    "build_plan_messages",
    "coerce_plan",
    "content_digest",
    "describe_models",
    "discover_commands",
    "extract_json_object",
    "fetch_github_issue",
    "ingest_repository",
    "initial_state",
    "load_solutions",
    "parse_github_repo",
    "parse_plan",
    "render_context",
    "resolve_commands",
    "resolve_issue",
    "run_agent",
    "split_model",
    "summarize",
    "targeted_test_command",
]
