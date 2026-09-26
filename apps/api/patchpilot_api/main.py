"""FastAPI application factory.

The API is versioned under ``/api/v1``. Domain errors are translated into a
single error envelope with a stable ``code``, a human message, a remediation and
the correlation id, so a client never has to parse a stack trace. Every error --
a domain error, a validation failure, a routing 404 or an unexpected exception
-- uses that same envelope.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from patchpilot_core.config import Settings, get_settings
from patchpilot_core.errors import PatchPilotError
from patchpilot_core.logging import configure_logging, get_logger, log_context
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from .db import get_engine
from .migrations import current_revision, ensure_schema, head_revision
from .routers import benchmarks, repositories, runs, system
from .security import check_startup, require_api_key, resolve_correlation_id
from .version import __version__
from .worker import get_worker

logger = get_logger(__name__, component="api")

API_PREFIX = "/api/v1"

DESCRIPTION = """
PatchPilot is an autonomous repository-level bug-fix agent and evaluation platform.

**What it does:** index a repository structurally, retrieve the relevant symbols
with their import and call-graph neighbours, ask a configurable model for a
plan and then a patch, run that patch inside a locked-down sandbox, feed the
failures back into a bounded repair loop, and persist every decision.

**What it does not do:** repair arbitrary software unattended. Every patch needs
human review before it is exported or merged, and the sandbox is Docker-based
isolation with the limits documented in `docs/security.md`.

**Authentication:** when the server sets `PATCHPILOT_API_KEYS`, every `/api/v1`
route needs one of those keys as `Authorization: Bearer <key>` or
`X-API-Key: <key>`.
"""

TAGS = [
    {"name": "runs", "description": "Create agent runs and inspect their state machine."},
    {"name": "repositories", "description": "Register, index and browse repositories."},
    {"name": "issues", "description": "Optional GitHub issue lookup."},
    {"name": "models", "description": "Which models are selectable and why."},
    {"name": "benchmarks", "description": "Datasets, benchmark runs and reports."},
    {"name": "artifacts", "description": "Diffs and sanitised sandbox logs."},
    {"name": "system", "description": "Runtime configuration, sandbox status, job queue."},
]

# Applied to every response. The API serves JSON and plain-text artifacts --
# sandbox logs that contain whatever the repository printed -- so nothing it
# returns should ever be sniffed as HTML or framed by another site.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
API_ONLY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
}


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


def _envelope(
    request: Request,
    status_code: int,
    *,
    code: str,
    message: str,
    remediation: str | None = None,
    context: dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    correlation_id = _correlation_id(request)
    # Set here as well as in the middleware: the handler for unexpected errors
    # runs outside the middleware stack, and its response must carry them too.
    merged = {**SECURITY_HEADERS, **(headers or {})}
    if correlation_id:
        merged["x-correlation-id"] = correlation_id
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "remediation": remediation,
            "context": context or {},
            "correlation_id": correlation_id,
        },
        headers=merged,
    )


def readiness(settings: Settings, *, worker_expected: bool) -> tuple[bool, dict[str, str]]:
    """Whether this process can do its job right now, check by check."""
    checks: dict[str, str] = {}
    try:
        with get_engine(settings).connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"unreachable: {type(exc).__name__}"
        return False, checks

    try:
        applied, head = current_revision(settings), head_revision(settings)
        checks["migrations"] = "ok" if applied == head else f"at {applied}, head is {head}"
    except Exception as exc:
        checks["migrations"] = f"unknown: {type(exc).__name__}"

    if worker_expected:
        checks["worker"] = "ok" if get_worker(settings).running else "not running"
    return all(value == "ok" for value in checks.values()), checks


def create_app(settings: Settings | None = None, *, start_worker: bool = True) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    check_startup(settings)
    settings.ensure_dirs()
    run_worker = start_worker and settings.worker_enabled

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        get_engine(settings)
        if settings.auto_migrate:
            ensure_schema(settings)
        elif current_revision(settings) != head_revision(settings):
            logger.error(
                "the database schema is behind; run `patchpilot db upgrade`",
                extra={"applied": current_revision(settings), "head": head_revision(settings)},
            )
        worker = get_worker(settings)
        if run_worker:
            worker.start()
        logger.info(
            "api started",
            extra={
                "version": __version__,
                "environment": settings.environment,
                "worker": run_worker,
                "auth": settings.auth_enabled,
            },
        )
        try:
            yield
        finally:
            if run_worker:
                worker.stop()

    app = FastAPI(
        title="PatchPilot API",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        correlation_id = resolve_correlation_id(request.headers.get("x-correlation-id"))
        request.state.correlation_id = correlation_id
        with log_context(correlation_id=correlation_id, path=request.url.path):
            response = await call_next(request)
        response.headers["x-correlation-id"] = correlation_id
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith(API_PREFIX):
            for name, value in API_ONLY_HEADERS.items():
                response.headers.setdefault(name, value)
        return response

    # Added last so it is the outermost middleware: CORS preflights are answered
    # before authentication, and a 401 still carries the CORS headers a browser
    # needs in order to read it.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-correlation-id"],
    )

    @app.exception_handler(PatchPilotError)
    async def handle_domain_error(request: Request, exc: PatchPilotError) -> JSONResponse:
        logger.warning("domain error", extra={"code": exc.code, "path": request.url.path})
        headers: dict[str, str] = {}
        if exc.http_status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if exc.http_status == 503:
            headers["Retry-After"] = "30"
        return _envelope(
            request,
            exc.http_status,
            code=exc.code,
            message=exc.message,
            remediation=exc.remediation,
            context=exc.context,
            headers=headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # pydantic puts the original exception object in ``ctx``, which is not JSON
        # serialisable and would leak internals anyway. Project each error down to
        # the three fields a client can act on.
        errors = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())),
                "message": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
            for error in exc.errors()
        ]
        return _envelope(
            request,
            422,
            code="validation_error",
            message="the request body did not validate",
            remediation="Check the field errors in `context.errors`.",
            context={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict):
            return _envelope(
                request,
                exc.status_code,
                code=str(detail.get("code", "http_error")),
                message=str(detail.get("message", "")),
                remediation=detail.get("remediation"),
                context=detail.get("context"),
                headers=exc.headers,
            )
        return _envelope(
            request,
            exc.status_code,
            code="not_found" if exc.status_code == 404 else "http_error",
            message=str(detail),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # The client gets the correlation id, never the exception text: that can
        # carry paths, SQL or configuration. The log line has the full trace.
        with log_context(correlation_id=_correlation_id(request)):
            logger.error("unhandled error", exc_info=exc, extra={"path": request.url.path})
        return _envelope(
            request,
            500,
            code="internal_error",
            message="an unexpected error occurred",
            remediation="Report the correlation id; the server log has the details.",
        )

    authenticated = [Depends(require_api_key)]
    app.include_router(system.public_router, prefix=API_PREFIX)
    for router in (
        system.router,
        runs.router,
        runs.artifact_router,
        repositories.router,
        repositories.issues_router,
        repositories.models_router,
        benchmarks.datasets_router,
        benchmarks.router,
    ):
        app.include_router(router, prefix=API_PREFIX, dependencies=authenticated)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "name": "PatchPilot",
            "version": __version__,
            "docs": "/docs",
            "api": API_PREFIX,
        }

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        """Liveness: the process is up and serving."""
        return {"status": "ok", "version": __version__}

    @app.get("/ready", include_in_schema=False)
    def ready() -> JSONResponse:
        """Readiness: the database answers, the schema is current, the worker runs."""
        ok, checks = readiness(settings, worker_expected=run_worker)
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ready" if ok else "not ready", "checks": checks},
        )

    return app


def __getattr__(name: str) -> FastAPI:
    """``patchpilot_api.main:app``, built on first access rather than on import.

    Importing this module used to construct the application as a side effect --
    configuring logging and creating data directories for whatever environment
    happened to be loaded. Uvicorn resolves ``main:app`` through ``getattr``, so
    building it lazily changes nothing for the server.
    """
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
