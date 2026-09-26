"""worker registry

Workers can run outside the API process, so they report their liveness and
their sandbox here for the API to read.

Revision ID: 8d1c3f0e6b27
Revises: 5b2e9d4c7a10
Create Date: 2026-09-26 05:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '8d1c3f0e6b27'
down_revision: str | None = '5b2e9d4c7a10'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('workers',
    sa.Column('id', sa.String(length=120), nullable=False),
    sa.Column('hostname', sa.String(length=255), nullable=False),
    sa.Column('pid', sa.Integer(), nullable=False),
    sa.Column('concurrency', sa.Integer(), nullable=False),
    sa.Column('sandbox', sa.JSON(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('workers', schema=None) as batch_op:
        batch_op.create_index('ix_workers_heartbeat', ['heartbeat_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('workers', schema=None) as batch_op:
        batch_op.drop_index('ix_workers_heartbeat')
    op.drop_table('workers')
