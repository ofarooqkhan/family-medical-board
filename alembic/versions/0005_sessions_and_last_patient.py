"""Remembered sign-in sessions and the last patient each person looked at.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(255), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("last_seen_at", sa.DateTime),
    )
    op.create_index("ix_sessions_user", "sessions", ["user_id"])
    op.add_column("users", sa.Column("last_patient_id", sa.Integer))


def downgrade() -> None:
    op.drop_column("users", "last_patient_id")
    op.drop_index("ix_sessions_user", table_name="sessions")
    op.drop_table("sessions")
