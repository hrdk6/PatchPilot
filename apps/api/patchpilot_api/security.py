"""Authentication, request identity and startup safety checks.

Authentication is a shared-secret API key, deliberately simple: PatchPilot is a
single-team tool, and anything richer (SSO, per-user quotas) belongs in a reverse
proxy in front of it. When ``PATCHPILOT_API_KEYS`` is empty the API is open,
which is the right default on a developer machine and the wrong one anywhere
else -- so production refuses to start that way unless the operator says an
authenticating proxy is in front (``PATCHPILOT_ALLOW_UNAUTHENTICATED=true``).
"""

from __future__ import annotations

import hmac
import re

from fastapi import Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from patchpilot_core.config import Settings, get_settings
from patchpilot_core.errors import AuthenticationError, ConfigurationError
from patchpilot_core.logging import get_logger, new_correlation_id

logger = get_logger(__name__, component="security")

# A client-supplied correlation id is written into every log line of the
# request, so it is accepted only if it cannot forge or break a log record.
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

MIN_KEY_LENGTH = 16

_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="bearer",
    description="An entry of PATCHPILOT_API_KEYS, sent as `Authorization: Bearer <key>`.",
)
_api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    scheme_name="apiKey",
    description="An entry of PATCHPILOT_API_KEYS, sent as `X-API-Key: <key>`.",
)


def resolve_correlation_id(header: str | None) -> str:
    if header and _CORRELATION_ID.match(header):
        return header
    return new_correlation_id()


def key_matches(presented: str, keys: list[str]) -> bool:
    """Constant-time comparison against every configured key.

    Every key is compared even after a match, so the response time does not
    reveal which entry matched or how many there are.
    """
    presented_bytes = presented.encode("utf-8")
    matched = False
    for key in keys:
        matched |= hmac.compare_digest(presented_bytes, key.encode("utf-8"))
    return matched


def _settings_for(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


def require_api_key(
    request: Request,
    bearer: HTTPAuthorizationCredentials | None = Security(_bearer),
    header_key: str | None = Security(_api_key_header),
) -> None:
    """FastAPI dependency guarding every ``/api/v1`` route."""
    settings = _settings_for(request)
    if not settings.auth_enabled:
        return
    presented = header_key or (bearer.credentials if bearer else None)
    if presented and key_matches(presented, settings.api_keys):
        return
    raise AuthenticationError(
        "a valid API key is required" if presented is None else "the API key was not accepted",
        remediation=(
            "Send one of the keys in PATCHPILOT_API_KEYS as `Authorization: Bearer <key>` "
            "or `X-API-Key: <key>`."
        ),
    )


def check_startup(settings: Settings) -> list[str]:
    """Refuse configurations that are unsafe to serve; return the ones worth a warning."""
    if settings.is_production and not settings.auth_enabled and not settings.allow_unauthenticated:
        raise ConfigurationError(
            "refusing to start in production without authentication",
            remediation=(
                "Set PATCHPILOT_API_KEYS to one or more random keys (for example "
                "`openssl rand -hex 32`), or set PATCHPILOT_ALLOW_UNAUTHENTICATED=true "
                "if an authenticating reverse proxy guards the API."
            ),
        )
    short = [key for key in settings.api_keys if len(key) < MIN_KEY_LENGTH]
    if short and settings.is_production:
        raise ConfigurationError(
            f"{len(short)} API key(s) are shorter than {MIN_KEY_LENGTH} characters",
            remediation="Generate keys with `openssl rand -hex 32`.",
        )

    warnings: list[str] = []
    if short:
        warnings.append(f"{len(short)} API key(s) are shorter than {MIN_KEY_LENGTH} characters")
    if settings.is_production:
        if settings.database_url.startswith("sqlite"):
            warnings.append(
                "SQLite in production supports one host only; use PostgreSQL "
                "(pip install 'patchpilot[postgres]') for anything shared"
            )
        if "*" in settings.cors_origins:
            warnings.append("PATCHPILOT_CORS_ORIGINS allows every origin")
        if settings.run_overrides_allowed:
            warnings.append(
                "PATCHPILOT_ALLOW_RUN_OVERRIDES is on: API callers can loosen the sandbox policy"
            )
        if settings.local_repositories_allowed and not settings.local_repository_roots:
            warnings.append(
                "local repository paths are accepted from anywhere on this host; "
                "set PATCHPILOT_LOCAL_REPOSITORY_ROOTS"
            )
    if settings.worker_lease_seconds <= 2 * settings.worker_heartbeat_seconds:
        warnings.append(
            "PATCHPILOT_WORKER_LEASE_SECONDS should be well above twice the heartbeat "
            "interval, or live jobs may be mistaken for abandoned ones"
        )
    for warning in warnings:
        logger.warning("configuration warning", extra={"warning": warning})
    return warnings
