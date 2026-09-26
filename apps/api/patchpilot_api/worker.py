"""The background worker.

Indexing and agent runs take minutes; HTTP handlers must not. Jobs go into the
``jobs`` table and this worker picks them up.

Why a database-backed queue rather than Celery or RQ: it needs no broker, it
survives a restart (a queued job is still queued), it is observable with one SQL
query, and it works identically in a test, in ``docker compose`` and in a single
container. The claim is a conditional UPDATE, so running several worker threads
-- or several processes, inside API replicas or as ``patchpilot worker`` -- is
safe. Swapping in a real broker means reimplementing :class:`Worker` against the
same ``jobs`` rows; nothing else changes.

**Leases.** A claimed job carries the claiming worker's id and a heartbeat that a
maintenance thread renews every ``worker_heartbeat_seconds``. Any worker may
requeue a RUNNING job whose heartbeat is older than ``worker_lease_seconds``:
its worker is gone. That sweep runs at startup and then periodically, so a
crashed process's work is picked up by whichever process is still alive.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from typing import Any

from patchpilot_core.config import Settings, get_settings
from patchpilot_core.enums import JobStatus
from patchpilot_core.errors import PatchPilotError
from patchpilot_core.logging import get_logger, log_context, new_correlation_id
from patchpilot_sandbox import select_sandbox

from . import retention, store
from .db import session_scope
from .services import JOB_HANDLERS, handle_job_failure

logger = get_logger(__name__, component="worker")

MAX_JOB_ATTEMPTS = 3


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class Worker:
    """A pool of threads draining the ``jobs`` table."""

    def __init__(self, settings: Settings | None = None, concurrency: int | None = None) -> None:
        self.settings = settings or get_settings()
        self.concurrency = concurrency or self.settings.worker_concurrency
        self.worker_id = new_worker_id()
        self._threads: list[threading.Thread] = []
        self._maintenance: threading.Thread | None = None
        self._stop = threading.Event()
        self._active: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        self.register()
        self.recover_orphans()
        self.sweep_debris()
        for number in range(self.concurrency):
            thread = threading.Thread(
                target=self._loop, name=f"patchpilot-worker-{number}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        self._maintenance = threading.Thread(
            target=self._maintain, name="patchpilot-worker-maintenance", daemon=True
        )
        self._maintenance.start()
        logger.info(
            "worker started", extra={"threads": self.concurrency, "worker_id": self.worker_id}
        )

    def recover_orphans(self) -> int:
        """Requeue work whose worker has died; close work that keeps killing its worker.

        This is what makes an interrupted run resumable: the run keeps its
        recorded history and the graph continues its transition log from where it
        stopped. Only a job with a stale lease is touched, so jobs that another
        live process is running are left alone.
        """
        try:
            with session_scope(self.settings) as session:
                recovery = store.requeue_orphaned_jobs(
                    session,
                    lease_seconds=self.settings.worker_lease_seconds,
                    max_attempts=MAX_JOB_ATTEMPTS,
                )
                requeued = [job.id for job in recovery.requeued]
                abandoned = [
                    (job.id, job.type, dict(job.payload or {}), job.error or "")
                    for job in recovery.abandoned
                ]
        except Exception:
            logger.warning("could not recover orphaned jobs", exc_info=True)
            return 0
        if requeued:
            logger.info(
                "requeued interrupted jobs", extra={"count": len(requeued), "job_ids": requeued}
            )
        for job_id, job_type, payload, error in abandoned:
            logger.error(
                "abandoned a job that kept interrupting its worker", extra={"job_id": job_id}
            )
            handle_job_failure(job_type, payload, error, self.settings)
        return len(requeued)

    # ---------------------------------------------------------------- registry
    def sandbox_report(self) -> dict[str, Any]:
        """The sandbox this worker would run commands in, as the API shows it."""
        try:
            return select_sandbox(self.settings).describe()
        except Exception as exc:  # pragma: no cover - defensive
            return {"backend": "unknown", "available": False, "isolated": False, "reason": str(exc)}

    def register(self) -> None:
        try:
            with session_scope(self.settings) as session:
                store.register_worker(
                    session,
                    self.worker_id,
                    hostname=socket.gethostname(),
                    pid=os.getpid(),
                    concurrency=self.concurrency,
                    sandbox=self.sandbox_report(),
                )
        except Exception:
            logger.warning("could not register the worker", exc_info=True)

    def deregister(self) -> None:
        try:
            with session_scope(self.settings) as session:
                store.deregister_worker(session, self.worker_id)
        except Exception:
            logger.warning("could not deregister the worker", exc_info=True)

    def sweep_debris(self) -> None:
        """Remove workspaces and staging directories a crash left behind."""
        try:
            report = retention.sweep_debris(self.settings)
        except Exception:
            logger.warning("could not sweep leftover workspaces", exc_info=True)
            return
        if report.workspaces or report.staging:
            logger.info("removed leftover workspaces", extra=report.as_dict())

    def apply_retention(self) -> None:
        """Delete history older than PATCHPILOT_RETENTION_DAYS, if it is set."""
        if self.settings.retention_days is None:
            return
        try:
            retention.cleanup(self.settings, older_than_days=self.settings.retention_days)
        except Exception:
            logger.warning("retention cleanup failed", exc_info=True)

    def stop(self, timeout: float = 10.0) -> None:
        """Stop claiming work and wait for in-flight jobs, up to ``timeout``.

        A job still running when the process then exits keeps its lease until it
        goes stale, after which another worker -- or this one after a restart --
        requeues it.
        """
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        if self._maintenance is not None:
            self._maintenance.join(timeout=timeout)
            self._maintenance = None
        self._threads.clear()
        self.deregister()
        with self._lock:
            unfinished = sorted(self._active)
        if unfinished:
            logger.warning(
                "worker stopped with jobs still running; they will be requeued once "
                "their lease expires",
                extra={"job_ids": unfinished},
            )
        logger.info("worker stopped")

    @property
    def running(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    def wait_for_idle(self, timeout: float = 120.0) -> bool:
        """Block until the queue is empty and nothing is in flight.

        Used by the CLI demo and by integration tests, which need a deterministic
        point at which a queued run has finished.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with session_scope(self.settings) as session:
                pending = store.count_jobs(session, JobStatus.QUEUED, JobStatus.RUNNING)
            with self._lock:
                active = len(self._active)
            if not pending and active == 0:
                return True
            time.sleep(0.1)
        return False

    # ----------------------------------------------------------- maintenance
    def _maintain(self) -> None:
        """Renew this worker's leases, and periodically sweep for dead workers' jobs."""
        heartbeat = self.settings.worker_heartbeat_seconds
        sweep_every = max(heartbeat, self.settings.worker_lease_seconds / 2)
        retain_every = self.settings.retention_interval_hours * 3600
        last_sweep = time.monotonic()
        # The first retention pass waits one heartbeat, not a whole interval, so
        # a worker that restarts often still gets round to it.
        last_retention = time.monotonic() - retain_every
        while not self._stop.wait(heartbeat):
            now = time.monotonic()
            sweep = now - last_sweep >= sweep_every
            self.heartbeat(report_sandbox=sweep)
            if sweep:
                last_sweep = now
                self.recover_orphans()
            if now - last_retention >= retain_every:
                last_retention = now
                self.apply_retention()

    def heartbeat(self, *, report_sandbox: bool = False) -> int:
        """Renew this worker's registration and the leases on its running jobs.

        Returns how many of its running jobs it still holds.
        """
        with self._lock:
            active = sorted(self._active)
        # Probed outside the transaction: `docker info` can take a while.
        sandbox = self.sandbox_report() if report_sandbox else None
        try:
            with session_scope(self.settings) as session:
                store.heartbeat_worker(session, self.worker_id, sandbox=sandbox)
                held = store.heartbeat_jobs(session, active, self.worker_id)
        except Exception:
            logger.warning("could not renew job leases", exc_info=True)
            return 0
        if active and held < len(active):
            logger.warning(
                "lost the lease on a running job; another worker may have requeued it",
                extra={"held": held, "running": len(active)},
            )
        return held

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            claimed = self._claim()
            if claimed is None:
                self._stop.wait(self.settings.worker_poll_seconds)
                continue
            job_id, job_type, payload, correlation = claimed
            try:
                self._execute(job_id, job_type, payload, correlation)
            finally:
                with self._lock:
                    self._active.discard(job_id)

    def _claim(self) -> tuple[str, str, dict[str, Any], str | None] | None:
        try:
            with session_scope(self.settings) as session:
                job = store.claim_next_job(session, worker_id=self.worker_id)
                if job is None:
                    return None
                claimed = job.id, job.type, dict(job.payload or {}), job.correlation_id
        except Exception:
            logger.warning("could not claim a job", exc_info=True)
            return None
        # The claim itself stamped the first heartbeat; from here on the
        # maintenance thread renews it.
        with self._lock:
            self._active.add(claimed[0])
        return claimed

    def _execute(
        self, job_id: str, job_type: str, payload: dict[str, Any], correlation: str | None
    ) -> None:
        handler = JOB_HANDLERS.get(job_type)
        with log_context(
            job_id=job_id, job_type=job_type, correlation_id=correlation or new_correlation_id()
        ):
            if handler is None:
                logger.error("no handler for job type", extra={"job_type": job_type})
                self._finish(job_id, JobStatus.FAILED, error=f"unknown job type {job_type!r}")
                return
            started = time.perf_counter()
            try:
                result = handler(payload, self.settings)
            except PatchPilotError as exc:
                logger.error("job failed", extra={"code": exc.code, "error": exc.message})
                if self._finish(job_id, JobStatus.FAILED, error=exc.message):
                    handle_job_failure(job_type, payload, exc.message, self.settings)
                return
            except Exception as exc:
                logger.exception("job failed unexpectedly")
                message = f"{type(exc).__name__}: {exc}"
                if self._finish(job_id, JobStatus.FAILED, error=message):
                    handle_job_failure(job_type, payload, message, self.settings)
                return
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.info("job finished", extra={"duration_ms": duration_ms})
            self._finish(job_id, JobStatus.SUCCEEDED, result=result)

    def _finish(
        self,
        job_id: str,
        status: JobStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        try:
            with session_scope(self.settings) as session:
                owned = store.finish_job(
                    session,
                    job_id,
                    status=status,
                    result=result,
                    error=error,
                    worker_id=self.worker_id,
                )
        except Exception:
            logger.warning("could not record job completion", extra={"job_id": job_id})
            return False
        if not owned:
            logger.warning(
                "job finished after its lease was lost; not overwriting the new owner",
                extra={"job_id": job_id},
            )
        return owned


_worker: Worker | None = None


def get_worker(settings: Settings | None = None) -> Worker:
    global _worker
    if _worker is None:
        _worker = Worker(settings)
    return _worker


def reset_worker() -> None:
    global _worker
    if _worker is not None:
        _worker.stop(timeout=2.0)
    _worker = None
