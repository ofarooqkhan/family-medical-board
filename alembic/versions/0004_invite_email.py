"""Invites can be bound to one email address.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
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
    # Guards: a hosted schema sync (Replit) may have added these already.
    if not _has_column("invites", "email"):
        op.add_column("invites", sa.Column("email", sa.String(255)))
    if not _has_column("invites", "note"):
        op.add_column("invites", sa.Column("note", sa.String(255)))


def downgrade() -> None:
    op.drop_column("invites", "note")
    op.drop_column("invites", "email")
