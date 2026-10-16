"""per-channel region (flood) scope

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-03 12:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("channels", sa.Column("flood_scope", sa.String(length=31), nullable=True))


def downgrade() -> None:
    op.drop_column("channels", "flood_scope")
