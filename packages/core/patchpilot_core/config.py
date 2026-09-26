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

from .models import SandboxLimits

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

    # ------------------------------------------------------------------ auth
    # Secrets are excluded from ``repr`` so a settings object that ends up in a
    # log line or a traceback does not carry them along.
    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list, repr=False)
    """Keys accepted on ``/api/v1``. Empty means the API is unauthenticated."""
    allow_unauthenticated: bool = False
    """Production refuses to start without API keys unless this is set, for
    deployments where an authenticating reverse proxy sits in front."""

    # --------------------------------------------------------------- storage
    database_url: str = "sqlite+pysqlite:///./patchpilot.db"
    database_pool_size: int = Field(default=10, ge=1)
    database_max_overflow: int = Field(default=20, ge=0)
    auto_migrate: bool = True
    """Apply migrations at API startup. Turn off when several replicas start at
    once and run ``patchpilot db upgrade`` as a release step instead."""
    data_dir: Path = REPO_ROOT / ".patchpilot"

    # ------------------------------------------------------------ vector store
    qdrant_url: str | None = None
    qdrant_api_key: str | None = Field(default=None, repr=False)
    qdrant_collection: str = "patchpilot_chunks"

    # -------------------------------------------------------------- embeddings
    embedding_provider: EmbeddingProviderName = "hash"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 256
    embedding_base_url: str | None = None
    embedding_api_key: str | None = Field(default=None, repr=False)

    # ------------------------------------------------------------- llm access
    default_model: str = "mock:deterministic"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str | None = Field(default=None, repr=False)
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    anthropic_api_key: str | None = Field(default=None, repr=False)
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = Field(default=3, ge=0, le=10)
    """Retries for rate limits, overload and transient network errors."""

    # ----------------------------------------------------------------- github
    github_token: str | None = Field(default=None, repr=False)
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

    # ------------------------------------------------------------- run policy
    # Upper bounds on what one API request may ask for. The defaults above are
    # what a run gets when it asks for nothing; these are the most it can get.
    sandbox_max_timeout_seconds: int = Field(default=3600, ge=1)
    sandbox_max_memory_mb: int = Field(default=16384, ge=64)
    sandbox_max_cpus: float = Field(default=16.0, gt=0)
    allow_run_overrides: bool | None = None
    """Let API callers loosen the server's safety policy per run: network access,
    another sandbox image, a weaker backend, a larger patch budget, write access
    to protected paths. Unset means allowed locally and refused in production."""
    allow_local_repositories: bool | None = None
    """Let API callers ingest a path on this host. Unset means allowed locally and
    refused in production, where a path such as ``/etc`` would otherwise be copied,
    indexed and shown back through the retrieval trace."""
    local_repository_roots: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    """When set, local repository paths must sit inside one of these directories."""

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
    worker_enabled: bool = True
    """Run the worker inside the API process. Turn off when ``patchpilot worker``
    runs as its own process."""
    worker_concurrency: int = Field(default=2, ge=1)
    worker_poll_seconds: float = 0.5
    worker_heartbeat_seconds: float = Field(default=10.0, gt=0)
    worker_lease_seconds: float = Field(default=60.0, gt=0)
    """A running job whose heartbeat is older than this belongs to a dead worker
    and is requeued. Must comfortably exceed ``worker_heartbeat_seconds``."""
    max_queued_jobs: int = Field(default=200, ge=1)
    """Backpressure: new work is refused with 503 while this many jobs wait."""

    # -------------------------------------------------------------- frontend
    # NoDecode: pydantic-settings would otherwise JSON-decode a list field before
    # any validator runs, so the documented comma-separated form
    # (``PATCHPILOT_CORS_ORIGINS=http://a,http://b``) would fail at startup.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    @field_validator("cors_origins", "api_keys", "local_repository_roots", mode="before")
    @classmethod
    def _split_list(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("local_repository_roots", mode="after")
    @classmethod
    def _resolve_roots(cls, value: list[Path]) -> list[Path]:
        return [Path(os.path.expandvars(str(root))).expanduser().resolve() for root in value]

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(os.path.expandvars(value)).expanduser()
        return value

    # ------------------------------------------------------------- derived
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)

    @property
    def run_overrides_allowed(self) -> bool:
        if self.allow_run_overrides is not None:
            return self.allow_run_overrides
        return not self.is_production

    @property
    def local_repositories_allowed(self) -> bool:
        if self.allow_local_repositories is not None:
            return self.allow_local_repositories
        return not self.is_production

    @property
    def local_sandbox_permitted(self) -> bool:
        """Mirrors the guard in ``LocalSubprocessSandbox.available``."""
        return self.allow_local_sandbox and not self.is_production

    def sandbox_limits(self, **overrides: object) -> SandboxLimits:
        """The sandbox limits a run gets from this server's configuration.

        Every limit an operator can set is applied here, so a run created through
        the API, the CLI or a benchmark is bounded the same way.
        """
        values: dict[str, object] = {
            "cpus": self.sandbox_cpus,
            "memory_mb": self.sandbox_memory_mb,
            "pids": self.sandbox_pids_limit,
            "timeout_seconds": self.sandbox_timeout_seconds,
            "max_output_bytes": self.sandbox_max_output_bytes,
            "network": self.sandbox_network,
            "user": self.sandbox_user,
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        return SandboxLimits.model_validate(values)

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
