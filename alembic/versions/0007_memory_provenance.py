"""Agent memories carry where they came from and can be edited.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
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
    cols = {
        "source_run_id": sa.Column("source_run_id", sa.Integer, sa.ForeignKey("board_runs.id", ondelete="SET NULL")),
        "source_consultation_id": sa.Column("source_consultation_id", sa.Integer, sa.ForeignKey("consultations.id", ondelete="SET NULL")),
        "created_by": sa.Column("created_by", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")),
        "updated_at": sa.Column("updated_at", sa.DateTime),
        "updated_by": sa.Column("updated_by", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")),
    }
    for name, col in cols.items():
        if not _has_column("doctor_memory", name):
            op.add_column("doctor_memory", col)
    if not _has_index("doctor_memory", "ix_doctor_memory_patient_doctor"):
        op.create_index("ix_doctor_memory_patient_doctor", "doctor_memory", ["patient_id", "doctor_type"])


def downgrade() -> None:
    op.drop_index("ix_doctor_memory_patient_doctor", table_name="doctor_memory")
    for col in ("updated_by", "updated_at", "created_by", "source_consultation_id", "source_run_id"):
        op.drop_column("doctor_memory", col)
