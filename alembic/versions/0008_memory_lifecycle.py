"""Memory lifecycle: active or superseded notes, family-wide lessons, confirmation date.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _insp():
    from sqlalchemy import inspect
    return inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in _insp().get_columns(table))


def _has_index(table: str, name: str) -> bool:
    return any(i["name"] == name for i in _insp().get_indexes(table))


def upgrade() -> None:
    cols = {
        "status": sa.Column("status", sa.String(20), server_default="active", nullable=False),
        "superseded_by": sa.Column("superseded_by", sa.Integer),
        "category": sa.Column("category", sa.String(40)),
        "last_confirmed_at": sa.Column("last_confirmed_at", sa.DateTime),
    }
    for name, col in cols.items():
        if not _has_column("doctor_memory", name):
            op.add_column("doctor_memory", col)
    if not _has_index("doctor_memory", "ix_doctor_memory_status"):
        op.create_index("ix_doctor_memory_status", "doctor_memory", ["status"])


def downgrade() -> None:
    op.drop_index("ix_doctor_memory_status", table_name="doctor_memory")
    for col in ("last_confirmed_at", "category", "superseded_by", "status"):
        op.drop_column("doctor_memory", col)
