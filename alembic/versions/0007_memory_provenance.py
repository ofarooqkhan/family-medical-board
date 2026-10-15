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


def upgrade() -> None:
    op.add_column("doctor_memory", sa.Column("source_run_id", sa.Integer, sa.ForeignKey("board_runs.id", ondelete="SET NULL")))
    op.add_column("doctor_memory", sa.Column("source_consultation_id", sa.Integer, sa.ForeignKey("consultations.id", ondelete="SET NULL")))
    op.add_column("doctor_memory", sa.Column("created_by", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")))
    op.add_column("doctor_memory", sa.Column("updated_at", sa.DateTime))
    op.add_column("doctor_memory", sa.Column("updated_by", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")))
    op.create_index("ix_doctor_memory_patient_doctor", "doctor_memory", ["patient_id", "doctor_type"])


def downgrade() -> None:
    op.drop_index("ix_doctor_memory_patient_doctor", table_name="doctor_memory")
    for col in ("updated_by", "updated_at", "created_by", "source_consultation_id", "source_run_id"):
        op.drop_column("doctor_memory", col)
