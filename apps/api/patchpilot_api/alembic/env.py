"""Alembic environment.

The database URL always comes from PatchPilot settings rather than alembic.ini,
so ``alembic upgrade head`` and the running application can never disagree about
which database they are talking to.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from patchpilot_api.orm import Base
from patchpilot_core.config import get_settings
from sqlalchemy import engine_from_config, pool

config = context.config
# alembic.ini's logging setup is for the standalone `alembic` CLI only. When the
# application migrates in-process (API startup, `patchpilot db ...`), fileConfig
# would disable every logger that already exists and replace the root handlers,
# silently switching off PatchPilot's structured logging for the process.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Batch mode makes ALTER TABLE work on SQLite, which does not support
            # most in-place column changes.
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
