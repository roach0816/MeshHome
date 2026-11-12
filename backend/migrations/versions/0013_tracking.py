"""Tracking: tracked devices and their positions, courses, and offline map tiles

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-11 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New tables only, so the previous release keeps working after a rollback.
    op.create_table(
        "tracked_devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("label", sa.String(64), nullable=True),
        sa.Column("color", sa.String(7), nullable=False),
        sa.Column("interval_s", sa.Integer(), nullable=False),
        sa.Column("polling", sa.Boolean(), nullable=False),
        sa.Column("show", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_detail", sa.String(300), nullable=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("battery_v", sa.Float(), nullable=True),
        sa.Column("advert_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "track_positions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tracked_devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("alt_m", sa.Float(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("battery_v", sa.Float(), nullable=True),
    )
    op.create_index("ix_track_positions_device_at", "track_positions", ["device_id", "at"])
    op.create_table(
        "courses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("color", sa.String(7), nullable=False),
        sa.Column("visible", sa.Boolean(), nullable=False),
        sa.Column("segments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("waypoints", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("distance_m", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "map_tiles",
        sa.Column("z", sa.Integer(), primary_key=True),
        sa.Column("x", sa.Integer(), primary_key=True),
        sa.Column("y", sa.Integer(), primary_key=True),
        sa.Column("content_type", sa.String(32), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("map_tiles")
    op.drop_table("courses")
    op.drop_index("ix_track_positions_device_at", table_name="track_positions")
    op.drop_table("track_positions")
    op.drop_table("tracked_devices")
