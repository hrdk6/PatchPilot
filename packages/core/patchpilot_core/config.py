"""Central, environment-driven configuration for every PatchPilot component.

Nothing here requires a cloud account. The defaults are chosen so that
``docker compose up`` (or even a plain ``uvicorn``) gives a fully functional
local system using the deterministic mock model, the local hash embedder and
the in-process vector store.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]

SandboxBackend = Literal["auto", "docker", "local"]
EmbeddingProviderName = Literal["hash", "openai"]


class Settings(BaseSettings):
    """Runtime settings. Every field can be overridden with a ``PATCHPILOT_*`` env var."""

    model_config = SettingsConfigDict(
        env_prefix="PATCHPILOT_",
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------- general
    environment: Literal["local", "ci", "production"] = "local"
    log_level: str = "INFO"
    log_json: bool = True

    # --------------------------------------------------------------- storage
    database_url: str = "sqlite+pysqlite:///./patchpilot.db"
    data_dir: Path = REPO_ROOT / ".patchpilot"

    # ------------------------------------------------------------ vector store
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    qdrant_collection: str = "patchpilot_chunks"

    # -------------------------------------------------------------- embeddings
    embedding_provider: EmbeddingProviderName = "hash"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 256
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None

    # ------------------------------------------------------------- llm access
    default_model: str = "mock:deterministic"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    anthropic_api_key: str | None = None
    llm_timeout_seconds: float = 120.0

    # ----------------------------------------------------------------- github
    github_token: str | None = None
    github_api_url: str = "https://api.github.com"

    # ---------------------------------------------------------------- sandbox
    sandbox_backend: SandboxBackend = "auto"
    sandbox_image: str = "python:3.12-slim-bookworm"
    sandbox_cpus: float = 1.0
    sandbox_memory_mb: int = 1024
    sandbox_pids_limit: int = 256
    sandbox_timeout_seconds: int = 180
    sandbox_setup_timeout_seconds: int = 300
    sandbox_network: Literal["none", "bridge"] = "none"
    sandbox_max_output_bytes: int = 64_000
    sandbox_user: str = "10001:10001"
    sandbox_workdir: str = "/workspace"
    sandbox_tmpfs_size_mb: int = 64
    allow_local_sandbox: bool = True
    """Permit the (non-isolating) local subprocess sandbox. See docs/security.md."""

    # ------------------------------------------------------------------ agent
    max_repair_attempts: int = 3
    max_patch_files: int = 10
    max_patch_lines: int = 400
    allow_patching_hidden_files: bool = False
    retrieval_top_k: int = 12
    retrieval_max_context_chars: int = 40_000

    # --------------------------------------------------------------- indexing
    index_max_file_bytes: int = 400_000
    index_max_files: int = 5_000

    # --------------------------------------------------------------- workers
    worker_concurrency: int = 2
    worker_poll_seconds: float = 0.5

    # -------------------------------------------------------------- frontend
    # NoDecode: pydantic-settings would otherwise JSON-decode a list field before
    # any validator runs, so the documented comma-separated form
    # (``PATCHPILOT_CORS_ORIGINS=http://a,http://b``) would fail at startup.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(os.path.expandvars(value)).expanduser()
        return value

    # ------------------------------------------------------------- derived
    @property
    def workspace_root(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def artifact_root(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def repo_cache_root(self) -> Path:
        return self.data_dir / "repos"

    @property
    def vector_store_root(self) -> Path:
        return self.data_dir / "vectors"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.workspace_root,
            self.artifact_root,
            self.repo_cache_root,
            self.vector_store_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cache-cleared in tests via ``reset_settings``)."""
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
