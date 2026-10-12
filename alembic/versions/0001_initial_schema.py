"""Initial schema: the eight tables the Replit app created, plus the agent roster.

This revision reproduces the schema that database.init_database() built on the
live Replit database before Alembic existed. A database that already has these
tables is stamped at this revision instead of running it.

Revision ID: 0001
Revises:
Create Date: 2026-10-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("CURRENT_TIMESTAMP")

# Seed roster. Prompts are rewritten in item 11; the roster changes in item 4.4.
DEFAULT_AGENTS = [
    {
        "agent_type": "neurologist",
        "display_name": "Stroke Neurologist",
        "role": "Stroke Neurologist",
        "goal": "Assess neurological status post-stroke, monitor recovery, adjust medications",
        "backstory": "Senior stroke neurologist with 20+ years experience in acute stroke care and rehabilitation.\n"
                     "Expert in anticoagulation management, stroke prevention, and post-stroke complications.\n"
                     "Always considers bleeding risk vs. clot prevention balance.",
    },
    {
        "agent_type": "cardiologist",
        "display_name": "Cardiologist",
        "role": "Cardiologist",
        "goal": "Monitor cardiac function, arrhythmias, and cardiovascular risk",
        "backstory": "Board-certified cardiologist specializing in cardiac complications post-stroke.\n"
                     "Expert in atrial fibrillation management, heart failure, and cardiac risk stratification.\n"
                     "Reviews Holter monitors, ECGs, and cardiac biomarkers.",
    },
    {
        "agent_type": "nephrologist",
        "display_name": "Nephrologist",
        "role": "Nephrologist",
        "goal": "Monitor kidney function and electrolyte balance",
        "backstory": "Nephrologist focused on acute kidney injury in elderly patients.\n"
                     "Expert in medication dosing adjustments for renal function.\n"
                     "Monitors creatinine, GFR, and electrolytes closely.",
    },
    {
        "agent_type": "endocrinologist",
        "display_name": "Endocrinologist",
        "role": "Endocrinologist",
        "goal": "Manage diabetes and blood sugar control",
        "backstory": "Endocrinologist specializing in diabetes management in acute illness.\n"
                     "Expert in insulin dosing, oral hypoglycemics, and stress hyperglycemia.\n"
                     "Monitors HbA1c trends and adjusts medications for optimal glycemic control.",
    },
    {
        "agent_type": "radiologist",
        "display_name": "Neuroradiologist",
        "role": "Neuroradiologist specializing in elderly care",
        "goal": "Analyze medical imaging including MRI, CT scans, X-rays, and DICOM files for diagnostic insights",
        "backstory": "Leading neuroradiologist recognized as one of the best in the US for elderly care imaging.\n"
                     "Expert in stroke imaging, brain MRI/CT interpretation, chest X-rays, and musculoskeletal imaging.\n"
                     "Specializes in age-related changes vs pathological findings in elderly patients.\n"
                     "Proficient in DICOM analysis and can identify subtle findings others might miss.",
    },
    {
        "agent_type": "coordinator",
        "display_name": "Medical Coordinator",
        "role": "Medical Coordinator",
        "goal": "Synthesize all specialist input into unified care plan",
        "backstory": "Chief Medical Officer coordinating complex multi-specialty care.\n"
                     "Resolves conflicts between specialists using evidence-based hierarchy.\n"
                     "Applies governance: Neurologist > Cardiologist > Nephrologist > Endocrinologist > Radiologist.\n"
                     "Produces clear, actionable recommendations with risk stratification.",
    },
]


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("email", sa.String(255)),
        sa.Column("first_name", sa.String(255)),
        sa.Column("last_name", sa.String(255)),
        sa.Column("profile_image_url", sa.Text()),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=NOW),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=NOW),
    )

    op.create_table(
        "patients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(255), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("age", sa.Integer()),
        sa.Column("weight_lbs", sa.REAL()),
        sa.Column("medications", sa.Text()),
        sa.Column("allergies", sa.Text()),
        sa.Column("medical_history", sa.Text()),
        sa.Column("stroke_date", sa.TIMESTAMP()),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=NOW),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=NOW),
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(255), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id", ondelete="CASCADE")),
        sa.Column("title", sa.String(255), server_default="New Conversation"),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=NOW),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=NOW),
    )

    op.create_table(
        "patient_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id", ondelete="CASCADE")),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_type", sa.String(50)),
        sa.Column("file_data", sa.LargeBinary()),
        sa.Column("extracted_text", sa.Text()),
        sa.Column("extracted_values", postgresql.JSONB()),
        sa.Column("uploaded_at", sa.TIMESTAMP(), server_default=NOW),
    )

    op.create_table(
        "vitals_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id", ondelete="CASCADE")),
        sa.Column("timestamp", sa.TIMESTAMP(), server_default=NOW),
        sa.Column("reading_type", sa.String(100), nullable=False),
        sa.Column("value", sa.REAL()),
        sa.Column("unit", sa.String(50)),
        sa.Column("source", sa.String(255)),
        sa.Column("notes", sa.Text()),
        sa.Column("raw_data", postgresql.JSONB()),
        sa.Column("previous_value", sa.REAL()),
        sa.Column("change_from_previous", sa.REAL()),
    )

    op.create_table(
        "doctor_agents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_type", sa.String(100), nullable=False, unique=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(255), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("backstory", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=NOW),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=NOW),
    )

    op.create_table(
        "doctor_memory",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id", ondelete="CASCADE")),
        sa.Column("doctor_type", sa.String(100), nullable=False),
        sa.Column("memory_type", sa.String(50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("importance", sa.Integer(), server_default=sa.text("5")),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=NOW),
        sa.Column("source_file_id", sa.Integer(), sa.ForeignKey("patient_files.id", ondelete="SET NULL")),
    )

    op.create_table(
        "chat_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id", ondelete="CASCADE")),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="CASCADE")),
        sa.Column("timestamp", sa.TIMESTAMP(), server_default=NOW),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("addressed_to", sa.String(100)),
        sa.Column("responding_doctor", sa.String(100)),
        sa.Column("metadata", postgresql.JSONB()),
    )

    seed = sa.table(
        "doctor_agents",
        sa.column("agent_type", sa.String),
        sa.column("display_name", sa.String),
        sa.column("role", sa.String),
        sa.column("goal", sa.Text),
        sa.column("backstory", sa.Text),
    )
    op.execute(
        postgresql.insert(seed).values(DEFAULT_AGENTS).on_conflict_do_nothing(index_elements=["agent_type"])
    )


def downgrade() -> None:
    for name in ("chat_history", "doctor_memory", "doctor_agents", "vitals_history",
                 "patient_files", "conversations", "patients", "users"):
        op.drop_table(name)
