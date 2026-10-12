"""Caregiver schema: accounts and roles, audit log, documents with pages,
extracted items awaiting review, medications with change history,
appointments, board runs, and the full agent roster.

Access model from this revision on: every active user of the deployment (one
family) can see every patient. Roles decide who may write: admin manages
users, editor changes records, viewer reads. patients.user_id now means
"created by" and is no longer an ownership filter.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("CURRENT_TIMESTAMP")

NO_ADVICE = (
    "You never give medical advice. You do not recommend, adjust, start, stop, or "
    "dose any medication or treatment, and you do not diagnose. You summarize what "
    "the records say, note changes over time, and list precise questions the family "
    "should ask the treating doctor. If asked for advice, decline in one sentence and "
    "point to the treating doctor."
)

# Roster per the owner's decision (HANDOFF 4.4). Keyword domains drive routing.
AGENTS = [
    ("neurologist", "Neurologist", True, 10,
     ["stroke", "seizure", "memory", "dementia", "parkinson", "tremor", "headache", "numbness", "mri brain", "ct head", "neuro"],
     "You review neurological records: stroke follow-up, cognition, movement, seizures, brain imaging reports."),
    ("cardiologist", "Cardiologist", True, 20,
     ["heart", "blood pressure", "bp", "ecg", "ekg", "echo", "atrial", "afib", "chest pain", "cholesterol", "statin", "anticoag", "cardio"],
     "You review cardiac records: blood pressure trends, rhythm, heart function, cardiac tests, cardiac medications as listed."),
    ("nephrologist", "Nephrologist", True, 30,
     ["kidney", "renal", "creatinine", "egfr", "gfr", "potassium", "sodium", "electrolyte", "dialysis", "urea", "nephro"],
     "You review kidney-related records: creatinine and eGFR trends, electrolytes, fluid status, kidney-relevant medications as listed."),
    ("endocrinologist", "Endocrinologist", True, 40,
     ["diabetes", "glucose", "sugar", "hba1c", "a1c", "insulin", "thyroid", "tsh", "cgm", "endo"],
     "You review endocrine records: glucose and HbA1c trends, thyroid tests, diabetes-related medications as listed."),
    ("gastroenterologist", "Gastroenterologist", True, 50,
     ["stomach", "liver", "bowel", "constipation", "diarrhea", "reflux", "gerd", "endoscopy", "colonoscopy", "swallow", "alt", "ast", "bilirubin", "gastro"],
     "You review digestive and liver records: liver tests, bowel symptoms as recorded, endoscopy reports, digestive medications as listed."),
    ("pulmonologist", "Pulmonologist", False, 60,
     ["lung", "breath", "copd", "asthma", "oxygen", "spo2", "pneumonia", "chest x-ray", "inhaler", "pulmo"],
     "You review respiratory records: oxygen saturation, chest imaging reports, breathing-related medications as listed."),
    ("urologist", "Urologist", True, 70,
     ["urine", "urinary", "bladder", "prostate", "psa", "catheter", "incontinence", "uti", "uro"],
     "You review urinary and prostate records: urine tests, PSA, bladder symptoms as recorded, related medications as listed."),
    ("ophthalmologist", "Ophthalmologist", True, 80,
     ["eye", "vision", "cataract", "glaucoma", "retina", "macular", "eye drop", "ophthal"],
     "You review eye records: vision findings, eye pressure, retinal reports, eye medications as listed."),
    ("spine", "Spine Specialist", True, 90,
     ["spine", "back pain", "neck pain", "disc", "sciatica", "vertebra", "stenosis", "mri spine", "ortho"],
     "You review spine and back records: imaging reports, pain as recorded, mobility notes, related medications as listed."),
    ("geriatrician", "Geriatrician", False, 100,
     ["fall", "frailty", "weight loss", "appetite", "mobility", "function", "geriatric"],
     "You review overall function in older adults: falls, weight, mobility, appetite, and how the conditions interact, as recorded."),
    ("geriatric_psychiatry", "Geriatric Psychiatry", False, 110,
     ["mood", "depression", "anxiety", "sleep", "agitation", "delirium", "behavior", "psych"],
     "You review mood, sleep, and behavior records and related medications as listed."),
    ("general_duty", "General Duty Doctor", True, 120,
     ["fever", "infection", "antibiotic", "general", "overall", "everything", "summary", "vaccine", "wound"],
     "You review the whole record for general medical matters not owned by another specialist: infections, vaccinations, wounds, general labs."),
    ("radiologist", "Radiologist", True, 130,
     ["x-ray", "xray", "ct", "mri", "ultrasound", "scan", "imaging", "dicom", "radiology"],
     "You read imaging reports and images: what the report states, how it compares with earlier imaging, and what remains unexplained."),
    ("pharmacist", "Pharmacist", True, 140,
     ["medication", "medicine", "drug", "tablet", "dose", "prescription", "interaction", "pharmacy", "side effect"],
     "You reconcile the medication list: duplicates, the same drug under two names, items from more than one prescriber, "
     "changes between prescriptions, and anything the family should confirm with the prescriber. You never suggest a dose or a change."),
    ("coordinator", "Care Coordinator", True, 1000,
     [],
     "You combine the specialists' structured outputs into one brief for the family: what changed, what the records show, "
     "what is unclear, and a single de-duplicated list of questions for each treating doctor."),
]


def _system_prompt(display_name: str, focus: str) -> str:
    return (
        f"You are the {display_name} on a family's record-review board for an elderly patient. "
        f"{focus} Work only from the records provided and cite the document and page for each observation. "
        f"{NO_ADVICE} Output must follow the JSON schema you are given."
    )


def upgrade() -> None:
    # Users: accounts and roles
    op.add_column("users", sa.Column("password_hash", sa.Text()))
    op.add_column("users", sa.Column("role", sa.String(20), server_default="viewer", nullable=False))
    op.add_column("users", sa.Column("display_name", sa.String(255)))
    op.add_column("users", sa.Column("language", sa.String(8), server_default="en", nullable=False))
    op.add_column("users", sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False))
    op.add_column("users", sa.Column("last_login_at", sa.TIMESTAMP()))
    op.create_index("users_email_lower_idx", "users", [sa.text("lower(email)")], unique=True,
                    postgresql_where=sa.text("email IS NOT NULL"))

    op.create_table(
        "invites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("role", sa.String(20), nullable=False, server_default="viewer"),
        sa.Column("created_by", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=NOW),
        sa.Column("expires_at", sa.TIMESTAMP()),
        sa.Column("used_by", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("used_at", sa.TIMESTAMP()),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity", sa.String(50)),
        sa.Column("entity_id", sa.Integer()),
        sa.Column("details", postgresql.JSONB()),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=NOW, nullable=False),
    )
    op.create_index("audit_log_patient_idx", "audit_log", ["patient_id", "created_at"])

    # Patients: more demographics
    op.add_column("patients", sa.Column("date_of_birth", sa.Date()))
    op.add_column("patients", sa.Column("sex", sa.String(20)))
    op.add_column("patients", sa.Column("primary_doctor", sa.String(255)))
    op.add_column("patients", sa.Column("notes", sa.Text()))
    op.add_column("patients", sa.Column("is_archived", sa.Boolean(), server_default=sa.text("false"), nullable=False))

    # Documents: patient_files grows into the document record
    op.add_column("patient_files", sa.Column("mime_type", sa.String(100)))
    op.add_column("patient_files", sa.Column("sha256", sa.String(64)))
    op.add_column("patient_files", sa.Column("page_count", sa.Integer()))
    op.add_column("patient_files", sa.Column("uploaded_by", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")))
    op.add_column("patient_files", sa.Column("document_kind", sa.String(30)))
    op.add_column("patient_files", sa.Column("document_date", sa.Date()))
    op.add_column("patient_files", sa.Column("status", sa.String(20), server_default="uploaded", nullable=False))
    op.add_column("patient_files", sa.Column("extraction", postgresql.JSONB()))
    op.add_column("patient_files", sa.Column("extraction_model", sa.String(100)))
    op.add_column("patient_files", sa.Column("extraction_error", sa.Text()))
    op.create_index("patient_files_patient_idx", "patient_files", ["patient_id", "uploaded_at"])

    op.create_table(
        "document_pages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("patient_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("image_png", sa.LargeBinary()),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("text", sa.Text()),
        sa.UniqueConstraint("file_id", "page_number", name="document_pages_file_page_key"),
    )

    op.create_table(
        "extracted_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("patient_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_number", sa.Integer()),
        sa.Column("item_type", sa.String(30), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.REAL()),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("reviewed_by", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reviewed_at", sa.TIMESTAMP()),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=NOW),
    )
    op.create_index("extracted_items_patient_status_idx", "extracted_items", ["patient_id", "status"])

    op.create_table(
        "medications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("generic_name", sa.String(255)),
        sa.Column("dose", sa.String(100)),
        sa.Column("route", sa.String(50)),
        sa.Column("frequency", sa.String(100)),
        sa.Column("indication", sa.String(255)),
        sa.Column("prescriber", sa.String(255)),
        sa.Column("start_date", sa.Date()),
        sa.Column("end_date", sa.Date()),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("source_file_id", sa.Integer(), sa.ForeignKey("patient_files.id", ondelete="SET NULL")),
        sa.Column("source_page", sa.Integer()),
        sa.Column("extracted_item_id", sa.Integer(), sa.ForeignKey("extracted_items.id", ondelete="SET NULL")),
        sa.Column("created_by", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=NOW),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=NOW),
    )
    op.create_index("medications_patient_idx", "medications", ["patient_id", "status"])

    op.create_table(
        "medication_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("medication_id", sa.Integer(), sa.ForeignKey("medications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("change_type", sa.String(20), nullable=False),
        sa.Column("before", postgresql.JSONB()),
        sa.Column("after", postgresql.JSONB()),
        sa.Column("changed_by", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("changed_at", sa.TIMESTAMP(), server_default=NOW),
        sa.Column("source_file_id", sa.Integer(), sa.ForeignKey("patient_files.id", ondelete="SET NULL")),
    )

    # Vitals and lab values: page link and reference range
    op.add_column("vitals_history", sa.Column("source_page", sa.Integer()))
    op.add_column("vitals_history", sa.Column("extracted_item_id", sa.Integer(),
                                              sa.ForeignKey("extracted_items.id", ondelete="SET NULL")))
    op.add_column("vitals_history", sa.Column("reference_range", sa.String(100)))
    op.add_column("vitals_history", sa.Column("flag", sa.String(10)))
    op.add_column("vitals_history", sa.Column("created_by", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")))
    op.create_index("vitals_history_patient_idx", "vitals_history", ["patient_id", "reading_type"])

    op.create_table(
        "appointments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(30), server_default="visit", nullable=False),
        sa.Column("doctor_name", sa.String(255)),
        sa.Column("specialty", sa.String(100)),
        sa.Column("location", sa.String(255)),
        sa.Column("starts_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("ends_at", sa.TIMESTAMP()),
        sa.Column("reminder_minutes", sa.Integer(), server_default=sa.text("1440")),
        sa.Column("status", sa.String(20), server_default="scheduled", nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=NOW),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=NOW),
    )
    op.create_index("appointments_patient_idx", "appointments", ["patient_id", "starts_at"])

    op.create_table(
        "board_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="SET NULL")),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("language", sa.String(8), server_default="en"),
        sa.Column("routed_agents", postgresql.JSONB()),
        sa.Column("specialist_outputs", postgresql.JSONB()),
        sa.Column("coordinator_output", postgresql.JSONB()),
        sa.Column("model_calls", postgresql.JSONB()),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0")),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0")),
        sa.Column("estimated_cost_usd", sa.REAL()),
        sa.Column("status", sa.String(20), server_default="done", nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=NOW),
    )
    op.create_index("board_runs_patient_idx", "board_runs", ["patient_id", "created_at"])

    # Agents: routing domains, per-agent model, rewritten prompts
    op.add_column("doctor_agents", sa.Column("domains", postgresql.JSONB()))
    op.add_column("doctor_agents", sa.Column("system_prompt", sa.Text()))
    op.add_column("doctor_agents", sa.Column("provider", sa.String(50)))
    op.add_column("doctor_agents", sa.Column("model", sa.String(100)))
    op.add_column("doctor_agents", sa.Column("sort_order", sa.Integer(), server_default=sa.text("500")))

    agents = sa.table(
        "doctor_agents",
        sa.column("agent_type", sa.String), sa.column("display_name", sa.String), sa.column("role", sa.String),
        sa.column("goal", sa.Text), sa.column("backstory", sa.Text), sa.column("is_active", sa.Boolean),
        sa.column("domains", postgresql.JSONB), sa.column("system_prompt", sa.Text), sa.column("sort_order", sa.Integer),
    )
    for agent_type, display_name, active, order, domains, focus in AGENTS:
        row = {
            "agent_type": agent_type,
            "display_name": display_name,
            "role": display_name,
            "goal": "Summarize the records in this domain and list questions for the treating doctor. Never advise.",
            "backstory": focus,
            "is_active": active,
            "domains": domains,
            "system_prompt": _system_prompt(display_name, focus),
            "sort_order": order,
        }
        stmt = postgresql.insert(agents).values(**row)
        # Existing rows get the new prompts: the old ones told agents to adjust
        # medications and recommend treatment, which the app must never do.
        stmt = stmt.on_conflict_do_update(
            index_elements=["agent_type"],
            set_={k: stmt.excluded[k] for k in ("display_name", "role", "goal", "backstory", "domains", "system_prompt", "sort_order")},
        )
        op.execute(stmt)
    op.execute("UPDATE doctor_agents SET updated_at = CURRENT_TIMESTAMP")


def downgrade() -> None:
    for col in ("domains", "system_prompt", "provider", "model", "sort_order"):
        op.drop_column("doctor_agents", col)
    op.drop_table("board_runs")
    op.drop_table("appointments")
    op.drop_index("vitals_history_patient_idx", table_name="vitals_history")
    for col in ("source_page", "extracted_item_id", "reference_range", "flag", "created_by"):
        op.drop_column("vitals_history", col)
    op.drop_table("medication_changes")
    op.drop_table("medications")
    op.drop_table("extracted_items")
    op.drop_table("document_pages")
    op.drop_index("patient_files_patient_idx", table_name="patient_files")
    for col in ("mime_type", "sha256", "page_count", "uploaded_by", "document_kind", "document_date",
                "status", "extraction", "extraction_model", "extraction_error"):
        op.drop_column("patient_files", col)
    for col in ("date_of_birth", "sex", "primary_doctor", "notes", "is_archived"):
        op.drop_column("patients", col)
    op.drop_table("audit_log")
    op.drop_table("invites")
    op.drop_index("users_email_lower_idx", table_name="users")
    for col in ("password_hash", "role", "display_name", "language", "is_active", "last_login_at"):
        op.drop_column("users", col)
