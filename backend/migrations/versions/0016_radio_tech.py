"""Meshtastic as a radio technology: which one each radio runs

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-13 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive: the previous release ignores the column (and only ever made MeshCore radios).
    op.add_column(
        "radios",
        sa.Column("tech", sa.String(16), nullable=False, server_default="meshcore"),
    )


def downgrade() -> None:
    op.drop_column("radios", "tech")
