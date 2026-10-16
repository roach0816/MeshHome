"""Sessions signed in from the mobile apps

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-06 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive and nullable, so the previous release keeps working after a rollback.
    op.add_column("sessions", sa.Column("client", sa.String(length=16), nullable=True))
    op.add_column("sessions", sa.Column("device_name", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "device_name")
    op.drop_column("sessions", "client")
