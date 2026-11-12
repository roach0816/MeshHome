"""Meshtastic trackers: the nodes a Meshtastic radio has heard, and trackers that are one of them

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-12 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive, so the previous release keeps working after a rollback: it joins trackers to
    # contacts, so it simply doesn't see the Meshtastic ones.
    op.create_table(
        "mesh_nodes",
        sa.Column("num", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("node_id", sa.String(9), nullable=False),
        sa.Column("long_name", sa.String(40), nullable=True),
        sa.Column("short_name", sa.String(8), nullable=True),
        sa.Column("hw_model", sa.Integer(), nullable=True),
        sa.Column("role", sa.String(16), nullable=True),
        sa.Column("public_key", sa.String(64), nullable=True),
        sa.Column("last_heard_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("alt_m", sa.Float(), nullable=True),
        sa.Column("position_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("battery_level", sa.Integer(), nullable=True),
        sa.Column("voltage", sa.Float(), nullable=True),
        sa.Column("snr", sa.Float(), nullable=True),
        sa.Column("hops_away", sa.Integer(), nullable=True),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.alter_column("tracked_devices", "contact_id", existing_type=postgresql.UUID(), nullable=True)
    op.add_column(
        "tracked_devices",
        sa.Column(
            "mesh_node_num",
            sa.BigInteger(),
            sa.ForeignKey("mesh_nodes.num", ondelete="CASCADE"),
            nullable=True,
            unique=True,
        ),
    )
    op.add_column("tracked_devices", sa.Column("battery_reported_pct", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_tracked_devices_one_source", "tracked_devices", "num_nonnulls(contact_id, mesh_node_num) = 1"
    )


def downgrade() -> None:
    op.execute("DELETE FROM tracked_devices WHERE contact_id IS NULL")
    op.drop_constraint("ck_tracked_devices_one_source", "tracked_devices", type_="check")
    op.drop_column("tracked_devices", "battery_reported_pct")
    op.drop_column("tracked_devices", "mesh_node_num")
    op.alter_column("tracked_devices", "contact_id", existing_type=postgresql.UUID(), nullable=False)
    op.drop_table("mesh_nodes")
