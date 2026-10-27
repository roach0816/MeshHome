"""Automatic retries of direct messages: each attempt's number, ACK code and route

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-27 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable columns only, so the previous release keeps working after a rollback.
    op.add_column("send_attempts", sa.Column("round_attempt", sa.Integer(), nullable=True))
    op.add_column("send_attempts", sa.Column("expected_ack", sa.String(16), nullable=True))
    op.add_column("send_attempts", sa.Column("flood", sa.Boolean(), nullable=True))
    op.create_index("ix_send_attempts_expected_ack", "send_attempts", ["expected_ack"])


def downgrade() -> None:
    op.drop_index("ix_send_attempts_expected_ack", table_name="send_attempts")
    op.drop_column("send_attempts", "flood")
    op.drop_column("send_attempts", "expected_ack")
    op.drop_column("send_attempts", "round_attempt")
