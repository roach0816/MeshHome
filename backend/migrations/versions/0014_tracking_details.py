"""Tracking details: a tracker's course, how each reply reached the radio, and sensor readings

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-12 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable columns only, so the previous release keeps working after a rollback.
    op.add_column(
        "tracked_devices",
        sa.Column(
            "course_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("courses.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("tracked_devices", sa.Column("battery_max_v", sa.Float(), nullable=True))
    for name, type_ in (
        ("snr", sa.Float()),
        ("rssi", sa.Float()),
        ("route", sa.String(8)),
        ("hops", postgresql.JSONB()),
        ("temp_c", sa.Float()),
        ("pressure_hpa", sa.Float()),
        ("baro_alt_m", sa.Float()),
    ):
        op.add_column("track_positions", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name in ("baro_alt_m", "pressure_hpa", "temp_c", "hops", "route", "rssi", "snr"):
        op.drop_column("track_positions", name)
    op.drop_column("tracked_devices", "battery_max_v")
    op.drop_column("tracked_devices", "course_id")
