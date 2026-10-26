"""Readings from sensors attached to this Pi (environment sensor, GPS)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-25 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A new table only, so the previous release keeps working after a rollback.
    op.create_table(
        "sensor_readings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("temperature_c", sa.Float(), nullable=True),
        sa.Column("humidity", sa.Float(), nullable=True),
        sa.Column("pressure_hpa", sa.Float(), nullable=True),
        sa.Column("gas_ohms", sa.Float(), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("alt_m", sa.Float(), nullable=True),
        sa.Column("satellites", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("sensor_readings")
