"""FastAPI application factory.

The API is versioned under ``/api/v1``. Domain errors are translated into a
single error envelope with a stable ``code``, a human message, a remediation and
the correlation id, so a client never has to parse a stack trace.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from patchpilot_core.config import Settings, get_settings
from patchpilot_core.errors import PatchPilotError
from patchpilot_core.logging import (
    configure_logging,
    get_logger,
    log_context,
    new_correlation_id,
)

from .db import get_engine
from .migrations import ensure_schema
from .routers import benchmarks, repositories, runs, system
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


def create_app(settings: Settings | None = None, *, start_worker: bool = True) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    settings.ensure_dirs()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        get_engine(settings)
        ensure_schema(settings)
        worker = get_worker(settings)
        if start_worker:
            worker.start()
        logger.info(
            "api started",
            extra={
                "version": __version__,
                "environment": settings.environment,
                "worker": start_worker,
            },
        )
        try:
            yield
        finally:
            if start_worker:
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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        correlation_id = request.headers.get("x-correlation-id") or new_correlation_id()
        with log_context(correlation_id=correlation_id, path=request.url.path):
            response = await call_next(request)
        response.headers["x-correlation-id"] = correlation_id
        return response

    @app.exception_handler(PatchPilotError)
    async def handle_domain_error(request: Request, exc: PatchPilotError) -> JSONResponse:
        logger.warning("domain error", extra={"code": exc.code, "path": request.url.path})
        payload = exc.to_dict()
        payload["correlation_id"] = request.headers.get("x-correlation-id")
        return JSONResponse(status_code=exc.http_status, content=payload)

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
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "the request body did not validate",
                "remediation": "Check the field errors in `context.errors`.",
                "context": {"errors": errors},
                "correlation_id": request.headers.get("x-correlation-id"),
            },
        )

    app.include_router(system.router, prefix=API_PREFIX)
    app.include_router(runs.router, prefix=API_PREFIX)
    app.include_router(runs.artifact_router, prefix=API_PREFIX)
    app.include_router(repositories.router, prefix=API_PREFIX)
    app.include_router(repositories.issues_router, prefix=API_PREFIX)
    app.include_router(repositories.models_router, prefix=API_PREFIX)
    app.include_router(benchmarks.datasets_router, prefix=API_PREFIX)
    app.include_router(benchmarks.router, prefix=API_PREFIX)

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
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
