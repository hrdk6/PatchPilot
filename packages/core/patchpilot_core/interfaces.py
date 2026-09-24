"""Structural interfaces between subsystems.

These are ``typing.Protocol`` definitions rather than ABCs so that packages stay
decoupled: ``patchpilot_agent`` depends on the *shape* of a sandbox, not on
``patchpilot_sandbox``. Adding a TypeScript parser, an E2B sandbox or another
model provider means implementing one of these protocols and registering it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import (
    ChatMessage,
    CodeChunk,
    CommandSpec,
    FileRecord,
    ImportEdge,
    LLMResponse,
    SandboxExecution,
    SandboxLimits,
    SymbolRef,
)


@runtime_checkable
class LLMAdapter(Protocol):
    """A chat-completion provider. Adapters must never raise raw provider errors."""

    name: str
    model: str

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse: ...


@runtime_checkable
class WorkspaceAware(Protocol):
    """Optional adapter capability: knowing which checkout a run is operating on.

    Only the deterministic test double implements this -- it replays scripted
    edits and needs the real file contents to build a diff that applies. Real
    providers see nothing but the prompt.
    """

    def bind_workspace(self, path: Path, repo_sha: str) -> None: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns code chunks and queries into vectors."""

    name: str
    dimension: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class VectorMatch(Protocol):
    chunk_id: str
    score: float


@runtime_checkable
class VectorStore(Protocol):
    """Minimal vector index surface needed by the retriever."""

    def ensure_collection(self, dimension: int) -> None: ...

    def upsert(
        self,
        namespace: str,
        chunk_ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, object]],
    ) -> None: ...

    def search(
        self, namespace: str, vector: list[float], limit: int
    ) -> list[tuple[str, float]]: ...

    def delete_namespace(self, namespace: str) -> None: ...


@runtime_checkable
class CodeParser(Protocol):
    """Language-specific structural parser.

    ``language`` and ``extensions`` drive parser selection; implement this
    protocol to add JavaScript/TypeScript support without touching the indexer.
    """

    language: str
    extensions: tuple[str, ...]

    def parse(
        self, path: str, source: str
    ) -> tuple[list[SymbolRef], list[ImportEdge], FileRecord]: ...

    def chunk(self, path: str, source: str, repo_sha: str) -> list[CodeChunk]: ...


@runtime_checkable
class Sandbox(Protocol):
    """Executes untrusted commands against a copy of the repository."""

    name: str

    def available(self) -> tuple[bool, str]:
        """Return ``(usable, human readable reason)`` without raising."""

    def run(
        self,
        workspace: Path,
        commands: list[CommandSpec],
        *,
        limits: SandboxLimits,
        env: dict[str, str] | None = None,
    ) -> SandboxExecution: ...
