"""Tracker telemetry: humidity, light and battery level per position; the latest readings

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-13 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive: the previous release ignores the columns.
    op.add_column("track_positions", sa.Column("humidity", sa.Float(), nullable=True))
    op.add_column("track_positions", sa.Column("lux", sa.Float(), nullable=True))
    op.add_column("track_positions", sa.Column("battery_pct", sa.Integer(), nullable=True))
    op.add_column("tracked_devices", sa.Column("telemetry", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("tracked_devices", "telemetry")
    op.drop_column("track_positions", "battery_pct")
    op.drop_column("track_positions", "lux")
    op.drop_column("track_positions", "humidity")
