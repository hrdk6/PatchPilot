"""Running Alembic migrations from inside the application.

Schema changes go through Alembic, never through ``create_all``: a portfolio
project that cannot upgrade an existing database is a demo, not a system. The API
runs ``upgrade head`` at startup, which is safe because migrations are
idempotent and the local deployment is a single instance. In a multi-replica
deployment you would run this as a separate step before rollout; the
``patchpilot db upgrade`` CLI command does exactly that.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from patchpilot_core.config import Settings, get_settings
from patchpilot_core.logging import get_logger

from .db import get_engine

logger = get_logger(__name__, component="migrations")

PACKAGE_ROOT = Path(__file__).resolve().parent
ALEMBIC_DIR = PACKAGE_ROOT / "alembic"
ALEMBIC_INI = PACKAGE_ROOT.parent / "alembic.ini"


def alembic_config(settings: Settings | None = None) -> Config:
    settings = settings or get_settings()
    config = Config(str(ALEMBIC_INI)) if ALEMBIC_INI.is_file() else Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    # Keep PatchPilot's own logging configuration; see alembic/env.py.
    config.attributes["configure_logger"] = False
    return config


def upgrade(settings: Settings | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(settings), revision)


def downgrade(settings: Settings | None = None, revision: str = "-1") -> None:
    command.downgrade(alembic_config(settings), revision)


def current_revision(settings: Settings | None = None) -> str | None:
    engine = get_engine(settings)
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision(settings: Settings | None = None) -> str | None:
    return ScriptDirectory.from_config(alembic_config(settings)).get_current_head()


def ensure_schema(settings: Settings | None = None) -> str | None:
    """Bring the database to the latest revision. Called at API startup."""
    settings = settings or get_settings()
    before = current_revision(settings)
    head = head_revision(settings)
    if before == head:
        return before
    logger.info("applying migrations", extra={"from": before, "to": head})
    upgrade(settings)
    after = current_revision(settings)
    logger.info("migrations applied", extra={"revision": after})
    return after
