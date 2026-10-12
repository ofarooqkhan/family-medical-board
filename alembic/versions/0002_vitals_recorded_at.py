"""vitals_history: add recorded_at and source_file_id.

recorded_at is when the reading was taken, from the document. NULL means the
document carried no usable time; the UI shows "unknown" rather than the
ingest time. timestamp keeps meaning ingest time. source_file_id links the
reading to the uploaded file it came from.

Both statements use IF NOT EXISTS so a database that already received these
columns from the pre-Alembic database.init_database() upgrades cleanly.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-12

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE vitals_history ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMP")
    op.execute(
        "ALTER TABLE vitals_history ADD COLUMN IF NOT EXISTS source_file_id INTEGER "
        "REFERENCES patient_files(id) ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE vitals_history DROP COLUMN IF EXISTS source_file_id")
    op.execute("ALTER TABLE vitals_history DROP COLUMN IF EXISTS recorded_at")
