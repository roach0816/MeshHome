"""Preferred repeater: a remembered route per contact, and which attempts used it

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-05 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable columns only, so the previous release keeps working after a rollback.
    op.add_column("contacts", sa.Column("routing", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("send_attempts", sa.Column("via_repeater", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("send_attempts", "via_repeater")
    op.drop_column("contacts", "routing")
