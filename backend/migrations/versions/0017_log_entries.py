"""MeshHome's log history, for Settings → Health & logs

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-13 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive: the previous release ignores the table.
    op.create_table(
        "log_entries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("first_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(8), nullable=False),
        sa.Column("component", sa.String(24), nullable=False),
        sa.Column("event", sa.String(24), nullable=True),
        sa.Column("logger", sa.String(80), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_log_entries_last_at", "log_entries", ["last_at"])


def downgrade() -> None:
    op.drop_index("ix_log_entries_last_at", table_name="log_entries")
    op.drop_table("log_entries")
