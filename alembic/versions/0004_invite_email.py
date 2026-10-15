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


def upgrade() -> None:
    op.add_column("invites", sa.Column("email", sa.String(255)))
    op.add_column("invites", sa.Column("note", sa.String(255)))


def downgrade() -> None:
    op.drop_column("invites", "note")
    op.drop_column("invites", "email")
