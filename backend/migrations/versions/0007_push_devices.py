"""Phones that receive push notifications through the relay

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-07 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A new table only, so the previous release keeps working after a rollback.
    op.create_table(
        "push_devices",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("token", sa.String(length=256), nullable=False),
        sa.Column("ticket", sa.String(length=128), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("dms", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("channels", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("push_devices")
