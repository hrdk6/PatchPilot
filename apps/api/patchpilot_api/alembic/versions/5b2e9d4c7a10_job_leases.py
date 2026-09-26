"""job leases: worker id and heartbeat

A RUNNING job used to be treated as orphaned whenever a worker started, which
is only true with exactly one worker process. The lease lets any number of
workers tell a live job from an abandoned one.

Revision ID: 5b2e9d4c7a10
Revises: 01aa0f50dff0
Create Date: 2026-09-26 05:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '5b2e9d4c7a10'
down_revision: str | None = '01aa0f50dff0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('worker_id', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('heartbeat_at')
        batch_op.drop_column('worker_id')
