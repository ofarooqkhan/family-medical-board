"""Doctor conversations: audio recordings with transcript, summary and actions.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
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
    if _has_table("consultations"):
        return
    op.create_table(
        "consultations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("patient_id", sa.Integer, sa.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", sa.Integer, sa.ForeignKey("patient_files.id", ondelete="SET NULL")),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("happened_on", sa.Date),
        sa.Column("doctor_name", sa.String(255)),
        sa.Column("specialty", sa.String(255)),
        sa.Column("transcript", sa.Text),
        sa.Column("segments", postgresql.JSONB),
        sa.Column("summary", postgresql.JSONB),
        sa.Column("status", sa.String(20), server_default="ready", nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("transcription_model", sa.String(100)),
        sa.Column("summary_model", sa.String(100)),
        sa.Column("cost_usd", sa.Numeric(10, 6)),
        sa.Column("duration_seconds", sa.Numeric(10, 2)),
        sa.Column("created_by", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.current_timestamp(), nullable=False),
    )
    op.create_index("ix_consultations_patient", "consultations", ["patient_id", "happened_on"])


def downgrade() -> None:
    op.drop_index("ix_consultations_patient", table_name="consultations")
    op.drop_table("consultations")
