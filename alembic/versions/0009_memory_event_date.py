"""Memory notes carry the date of the thing they describe (a report's date, a visit's date).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    from sqlalchemy import inspect
    return any(c["name"] == column for c in inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_column("doctor_memory", "event_date"):
        op.add_column("doctor_memory", sa.Column("event_date", sa.Date))


def downgrade() -> None:
    op.drop_column("doctor_memory", "event_date")
