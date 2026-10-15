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


def _insp():
    from sqlalchemy import inspect
    return inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _insp().get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in _insp().get_columns(table))


def _has_index(table: str, name: str) -> bool:
    return any(i["name"] == name for i in _insp().get_indexes(table))


def upgrade() -> None:
    if _has_table("sessions"):
        if not _has_column("users", "last_patient_id"):
            op.add_column("users", sa.Column("last_patient_id", sa.Integer))
        return
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(255), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("last_seen_at", sa.DateTime),
    )
    op.create_index("ix_sessions_user", "sessions", ["user_id"])
    if not _has_column("users", "last_patient_id"):
        op.add_column("users", sa.Column("last_patient_id", sa.Integer))


def downgrade() -> None:
    op.drop_column("users", "last_patient_id")
    op.drop_index("ix_sessions_user", table_name="sessions")
    op.drop_table("sessions")
