"""The background worker.

Indexing and agent runs take minutes; HTTP handlers must not. Jobs go into the
``jobs`` table and this worker picks them up.

Why a database-backed queue rather than Celery or RQ: it needs no broker, it
survives a restart (a queued job is still queued), it is observable with one SQL
query, and it works identically in a test, in ``docker compose`` and in a single
container. The claim is a conditional UPDATE, so running several worker threads
-- or several API processes -- is safe. Swapping in a real broker means
reimplementing :class:`Worker` against the same ``jobs`` rows; nothing else
changes.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from patchpilot_core.config import Settings, get_settings
from patchpilot_core.enums import JobStatus
from patchpilot_core.errors import PatchPilotError
from patchpilot_core.logging import get_logger, log_context, new_correlation_id

from . import store
from .db import session_scope
from .services import JOB_HANDLERS

logger = get_logger(__name__, component="worker")


class Worker:
    """A pool of threads draining the ``jobs`` table."""

    def __init__(self, settings: Settings | None = None, concurrency: int | None = None) -> None:
        self.settings = settings or get_settings()
        self.concurrency = concurrency or self.settings.worker_concurrency
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._active = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        self.recover_orphans()
        for number in range(self.concurrency):
            thread = threading.Thread(
                target=self._loop, name=f"patchpilot-worker-{number}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        logger.info("worker started", extra={"threads": self.concurrency})

    def recover_orphans(self) -> int:
        """Requeue work stranded by a previous process.

        This is what makes an interrupted run resumable: the worker is the only
        thing that sets a job to RUNNING, so any job still RUNNING at startup
        belongs to a process that is gone. The run keeps its recorded history and
        the graph continues its transition log from where it stopped.
        """
        try:
            with session_scope(self.settings) as session:
                requeued = store.requeue_orphaned_jobs(session)
                identifiers = [job.id for job in requeued]
        except Exception:
            logger.warning("could not recover orphaned jobs", exc_info=True)
            return 0
        if identifiers:
            logger.info(
                "requeued interrupted jobs",
                extra={"count": len(identifiers), "job_ids": identifiers},
            )
        return len(identifiers)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()
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
                pending = [
                    job
                    for job in store.list_jobs(session, limit=200)
                    if job.status in (str(JobStatus.QUEUED), str(JobStatus.RUNNING))
                ]
            with self._lock:
                active = self._active
            if not pending and active == 0:
                return True
            time.sleep(0.1)
        return False

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            claimed = self._claim()
            if claimed is None:
                self._stop.wait(self.settings.worker_poll_seconds)
                continue
            job_id, job_type, payload, correlation = claimed
            with self._lock:
                self._active += 1
            try:
                self._execute(job_id, job_type, payload, correlation)
            finally:
                with self._lock:
                    self._active -= 1

    def _claim(self) -> tuple[str, str, dict[str, Any], str | None] | None:
        try:
            with session_scope(self.settings) as session:
                job = store.claim_next_job(session)
                if job is None:
                    return None
                return job.id, job.type, dict(job.payload or {}), job.correlation_id
        except Exception:
            logger.warning("could not claim a job", exc_info=True)
            return None

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
                self._finish(job_id, JobStatus.FAILED, error=exc.message)
                return
            except Exception as exc:
                logger.exception("job failed unexpectedly")
                self._finish(job_id, JobStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
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
    ) -> None:
        try:
            with session_scope(self.settings) as session:
                store.finish_job(session, job_id, status=status, result=result, error=error)
        except Exception:
            logger.warning("could not record job completion", extra={"job_id": job_id})


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
