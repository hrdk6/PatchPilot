"""Database engine and session management.

SQLite for local development, PostgreSQL for anything shared. The ORM layer uses
no SQLite-specific types, and the two behavioural differences that bite in
practice are handled here: foreign keys are off by default in SQLite (turned on
below) and the default SQLite journal mode serialises readers against a writer
(WAL mode fixes it, which matters because the background worker writes while the
API reads).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from patchpilot_core.config import Settings, get_settings
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


def build_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    url = settings.database_url
    kwargs: dict[str, object] = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        path = url.split("///")[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        _configure_sqlite(engine)
    return engine


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine, _session_factory
    if _engine is None:
        _engine = build_engine(settings)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    get_engine(settings)
    assert _session_factory is not None
    return _session_factory


def reset_engine() -> None:
    """Drop the cached engine. Used by tests that swap the database URL."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """A transactional session. Commits on success, rolls back on any exception."""
    factory = get_session_factory(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session
