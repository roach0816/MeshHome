"""Signed channel messages: the verified signer of a message, and per-channel signing settings

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-20 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New nullable / defaulted columns only, so the previous release keeps working after a rollback.
    op.add_column("messages", sa.Column("signed_by", sa.String(length=64), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("sign_messages", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "conversations",
        sa.Column("contacts_only", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("conversations", "contacts_only")
    op.drop_column("conversations", "sign_messages")
    op.drop_column("messages", "signed_by")
