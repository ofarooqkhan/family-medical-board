"""Postgres access layer.

Every read or write that touches patient data takes a user_id and checks that
the user may see that patient. Functions raise PermissionError when the check
fails so callers cannot silently operate on someone else's record.

A0 note: orphan patients (user_id IS NULL) are still visible to any signed-in
user. Removing that and adding the leakage test is item 2.
"""
from __future__ import annotations

import json
import os
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from utils import json_dumps_safe

DATABASE_URL = os.environ.get("DATABASE_URL")

SPECIALIST_ORDER = ["neurologist", "cardiologist", "nephrologist", "endocrinologist", "radiologist"]


def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_database() -> None:
    """Create tables if missing. Replaced by Alembic in item 1."""
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id VARCHAR(255) PRIMARY KEY,
            email VARCHAR(255),
            first_name VARCHAR(255),
            last_name VARCHAR(255),
            profile_image_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS patients (
            id SERIAL PRIMARY KEY,
            user_id VARCHAR(255) REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR(255) NOT NULL,
            age INTEGER,
            weight_lbs REAL,
            medications TEXT,
            allergies TEXT,
            medical_history TEXT,
            stroke_date TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            id SERIAL PRIMARY KEY,
            user_id VARCHAR(255) REFERENCES users(id) ON DELETE CASCADE,
            patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
            title VARCHAR(255) DEFAULT 'New Conversation',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS patient_files (
            id SERIAL PRIMARY KEY,
            patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
            filename VARCHAR(255) NOT NULL,
            file_type VARCHAR(50),
            file_data BYTEA,
            extracted_text TEXT,
            extracted_values JSONB,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS vitals_history (
            id SERIAL PRIMARY KEY,
            patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reading_type VARCHAR(100) NOT NULL,
            value REAL,
            unit VARCHAR(50),
            source VARCHAR(255),
            notes TEXT,
            raw_data JSONB,
            previous_value REAL,
            change_from_previous REAL
        )
    ''')
    # A0: recorded_at is the time the reading was taken (from the document).
    # timestamp stays as the time the row was ingested. NULL recorded_at means
    # the document did not carry a usable time; the UI must show that, not now().
    cur.execute('ALTER TABLE vitals_history ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMP')
    cur.execute('ALTER TABLE vitals_history ADD COLUMN IF NOT EXISTS source_file_id INTEGER '
                'REFERENCES patient_files(id) ON DELETE SET NULL')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS doctor_agents (
            id SERIAL PRIMARY KEY,
            agent_type VARCHAR(100) UNIQUE NOT NULL,
            display_name VARCHAR(255) NOT NULL,
            role VARCHAR(255) NOT NULL,
            goal TEXT NOT NULL,
            backstory TEXT NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS doctor_memory (
            id SERIAL PRIMARY KEY,
            patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
            doctor_type VARCHAR(100) NOT NULL,
            memory_type VARCHAR(50) NOT NULL,
            content TEXT NOT NULL,
            importance INTEGER DEFAULT 5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            source_file_id INTEGER REFERENCES patient_files(id) ON DELETE SET NULL
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS chat_history (
            id SERIAL PRIMARY KEY,
            patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
            conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            role VARCHAR(50) NOT NULL,
            content TEXT NOT NULL,
            addressed_to VARCHAR(100),
            responding_doctor VARCHAR(100),
            metadata JSONB
        )
    ''')

    init_default_agents(cur)

    conn.commit()
    cur.close()
    conn.close()


def init_default_agents(cur) -> None:
    """Seed the agent roster if empty. Prompts are rewritten in item 11."""
    default_agents = [
        {
            'agent_type': 'neurologist',
            'display_name': 'Stroke Neurologist',
            'role': 'Stroke Neurologist',
            'goal': 'Assess neurological status post-stroke, monitor recovery, adjust medications',
            'backstory': '''Senior stroke neurologist with 20+ years experience in acute stroke care and rehabilitation.
Expert in anticoagulation management, stroke prevention, and post-stroke complications.
Always considers bleeding risk vs. clot prevention balance.'''
        },
        {
            'agent_type': 'cardiologist',
            'display_name': 'Cardiologist',
            'role': 'Cardiologist',
            'goal': 'Monitor cardiac function, arrhythmias, and cardiovascular risk',
            'backstory': '''Board-certified cardiologist specializing in cardiac complications post-stroke.
Expert in atrial fibrillation management, heart failure, and cardiac risk stratification.
Reviews Holter monitors, ECGs, and cardiac biomarkers.'''
        },
        {
            'agent_type': 'nephrologist',
            'display_name': 'Nephrologist',
            'role': 'Nephrologist',
            'goal': 'Monitor kidney function and electrolyte balance',
            'backstory': '''Nephrologist focused on acute kidney injury in elderly patients.
Expert in medication dosing adjustments for renal function.
Monitors creatinine, GFR, and electrolytes closely.'''
        },
        {
            'agent_type': 'endocrinologist',
            'display_name': 'Endocrinologist',
            'role': 'Endocrinologist',
            'goal': 'Manage diabetes and blood sugar control',
            'backstory': '''Endocrinologist specializing in diabetes management in acute illness.
Expert in insulin dosing, oral hypoglycemics, and stress hyperglycemia.
Monitors HbA1c trends and adjusts medications for optimal glycemic control.'''
        },
        {
            'agent_type': 'radiologist',
            'display_name': 'Neuroradiologist',
            'role': 'Neuroradiologist specializing in elderly care',
            'goal': 'Analyze medical imaging including MRI, CT scans, X-rays, and DICOM files for diagnostic insights',
            'backstory': '''Leading neuroradiologist recognized as one of the best in the US for elderly care imaging.
Expert in stroke imaging, brain MRI/CT interpretation, chest X-rays, and musculoskeletal imaging.
Specializes in age-related changes vs pathological findings in elderly patients.
Proficient in DICOM analysis and can identify subtle findings others might miss.'''
        },
        {
            'agent_type': 'coordinator',
            'display_name': 'Medical Coordinator',
            'role': 'Medical Coordinator',
            'goal': 'Synthesize all specialist input into unified care plan',
            'backstory': '''Chief Medical Officer coordinating complex multi-specialty care.
Resolves conflicts between specialists using evidence-based hierarchy.
Applies governance: Neurologist > Cardiologist > Nephrologist > Endocrinologist > Radiologist.
Produces clear, actionable recommendations with risk stratification.'''
        }
    ]

    for agent in default_agents:
        cur.execute('''
            INSERT INTO doctor_agents (agent_type, display_name, role, goal, backstory)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (agent_type) DO NOTHING
        ''', (agent['agent_type'], agent['display_name'], agent['role'], agent['goal'], agent['backstory']))


# ---------------------------------------------------------------------------
# Access control helpers
# ---------------------------------------------------------------------------

def _patient_visible_clause() -> str:
    # Orphan rule kept for A0. Item 2 removes "OR p.user_id IS NULL".
    return "(p.user_id = %s OR p.user_id IS NULL)"


def _assert_patient_access(cur, patient_id: int, user_id: str) -> None:
    if not user_id:
        raise PermissionError("user_id is required")
    cur.execute(
        f'SELECT 1 FROM patients p WHERE p.id = %s AND {_patient_visible_clause()}',
        (patient_id, user_id),
    )
    if cur.fetchone() is None:
        raise PermissionError(f"user {user_id} may not access patient {patient_id}")


def _assert_conversation_access(cur, conversation_id: int, user_id: str) -> dict:
    if not user_id:
        raise PermissionError("user_id is required")
    cur.execute('SELECT * FROM conversations WHERE id = %s AND user_id = %s', (conversation_id, user_id))
    conv = cur.fetchone()
    if conv is None:
        raise PermissionError(f"user {user_id} may not access conversation {conversation_id}")
    return conv


def verify_patient_access(patient_id: int, user_id: str) -> bool:
    if not user_id:
        return False
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        _assert_patient_access(cur, patient_id, user_id)
        return True
    except PermissionError:
        return False
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_or_create_user(user_id, email=None, first_name=None, last_name=None, profile_image_url=None):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE id = %s', (user_id,))
    user = cur.fetchone()
    if not user:
        clean_email = email if email and email.strip() else None
        cur.execute('''
            INSERT INTO users (id, email, first_name, last_name, profile_image_url)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
        ''', (user_id, clean_email, first_name, last_name, profile_image_url))
        user = cur.fetchone()
        conn.commit()
    cur.close()
    conn.close()
    return user


def get_user(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE id = %s', (user_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user


def migrate_orphan_patients_to_user(user_id):
    """Assign patients with no owner to this user. Removed in item 2."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('UPDATE patients SET user_id = %s WHERE user_id IS NULL', (user_id,))
    affected = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return affected


# ---------------------------------------------------------------------------
# Patients
# ---------------------------------------------------------------------------

def get_all_patients_for_user_or_orphans(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM patients WHERE user_id = %s OR user_id IS NULL ORDER BY updated_at DESC', (user_id,))
    patients = cur.fetchall()
    cur.close()
    conn.close()
    return patients


def get_user_patients(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM patients WHERE user_id = %s ORDER BY updated_at DESC', (user_id,))
    patients = cur.fetchall()
    cur.close()
    conn.close()
    return patients


def get_patient(patient_id, user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(f'SELECT * FROM patients p WHERE p.id = %s AND {_patient_visible_clause()}', (patient_id, user_id))
    patient = cur.fetchone()
    cur.close()
    conn.close()
    return patient


def create_patient_for_user(user_id, name, age=None, weight_lbs=None, medications=None,
                            allergies=None, medical_history=None, stroke_date=None):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO patients (user_id, name, age, weight_lbs, medications, allergies, medical_history, stroke_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    ''', (user_id, name, age, weight_lbs, medications, allergies, medical_history, stroke_date))
    patient_id = cur.fetchone()['id']
    conn.commit()
    cur.close()
    conn.close()
    return patient_id


_PATIENT_EDITABLE = {'name', 'age', 'weight_lbs', 'medications', 'allergies', 'medical_history', 'stroke_date'}


def update_patient(patient_id, user_id, **kwargs):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        _assert_patient_access(cur, patient_id, user_id)
        set_clauses = []
        values: list[Any] = []
        for key, value in kwargs.items():
            if key in _PATIENT_EDITABLE:
                set_clauses.append(f"{key} = %s")
                values.append(value)
        if set_clauses:
            set_clauses.append("updated_at = CURRENT_TIMESTAMP")
            values.append(patient_id)
            cur.execute(f"UPDATE patients SET {', '.join(set_clauses)} WHERE id = %s", values)
            conn.commit()
    finally:
        cur.close()
        conn.close()


def delete_patient(patient_id, user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        _assert_patient_access(cur, patient_id, user_id)
        cur.execute('DELETE FROM patients WHERE id = %s', (patient_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def save_patient_file(patient_id, user_id, filename, file_type, file_data, extracted_text=None, extracted_values=None):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        _assert_patient_access(cur, patient_id, user_id)
        cur.execute('''
            INSERT INTO patient_files (patient_id, filename, file_type, file_data, extracted_text, extracted_values)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (patient_id, filename, file_type, psycopg2.Binary(file_data), extracted_text,
              json_dumps_safe(extracted_values)))
        file_id = cur.fetchone()['id']
        conn.commit()
        return file_id
    finally:
        cur.close()
        conn.close()


def get_patient_files(patient_id, user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(f'''
        SELECT pf.id, pf.filename, pf.file_type, pf.extracted_text, pf.extracted_values, pf.uploaded_at
        FROM patient_files pf
        JOIN patients p ON pf.patient_id = p.id
        WHERE pf.patient_id = %s AND {_patient_visible_clause()}
        ORDER BY pf.uploaded_at DESC
    ''', (patient_id, user_id))
    files = cur.fetchall()
    cur.close()
    conn.close()
    return files


def get_file_data(file_id, user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(f'''
        SELECT pf.file_data, pf.filename, pf.file_type, pf.patient_id
        FROM patient_files pf
        JOIN patients p ON pf.patient_id = p.id
        WHERE pf.id = %s AND {_patient_visible_clause()}
    ''', (file_id, user_id))
    file = cur.fetchone()
    cur.close()
    conn.close()
    return file


# ---------------------------------------------------------------------------
# Vitals
# ---------------------------------------------------------------------------

def save_vital_reading(patient_id, user_id, reading_type, value, unit, source="Manual Entry",
                       notes="", raw_data=None, recorded_at=None, source_file_id=None):
    """Save one reading. recorded_at is when it was taken; None means unknown."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        _assert_patient_access(cur, patient_id, user_id)
        cur.execute('''
            SELECT value FROM vitals_history
            WHERE patient_id = %s AND reading_type = %s
            ORDER BY COALESCE(recorded_at, timestamp) DESC LIMIT 1
        ''', (patient_id, reading_type))
        prev = cur.fetchone()
        previous_value = prev['value'] if prev else None
        change = value - previous_value if previous_value is not None else None

        cur.execute('''
            INSERT INTO vitals_history
            (patient_id, reading_type, value, unit, source, notes, raw_data, previous_value,
             change_from_previous, recorded_at, source_file_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (patient_id, reading_type, value, unit, source, notes, json_dumps_safe(raw_data),
              previous_value, change, recorded_at, source_file_id))
        row_id = cur.fetchone()['id']
        conn.commit()
        return row_id
    finally:
        cur.close()
        conn.close()


def save_extracted_readings(patient_id, user_id, file_id, filename, rows) -> int:
    """Persist rows produced by utils.normalize_extracted_readings."""
    count = 0
    for row in rows:
        save_vital_reading(
            patient_id, user_id,
            row["reading_type"], row["value"], row["unit"],
            source=f"Extracted: {filename}",
            notes=row.get("notes") or "",
            raw_data=row.get("raw"),
            recorded_at=row.get("recorded_at"),
            source_file_id=file_id,
        )
        count += 1
    return count


def get_vitals_history(patient_id, user_id, reading_type=None):
    conn = get_db_connection()
    cur = conn.cursor()
    base = f'''
        SELECT vh.* FROM vitals_history vh
        JOIN patients p ON vh.patient_id = p.id
        WHERE vh.patient_id = %s AND {_patient_visible_clause()}
    '''
    if reading_type:
        cur.execute(base + ' AND vh.reading_type = %s ORDER BY COALESCE(vh.recorded_at, vh.timestamp) DESC',
                    (patient_id, user_id, reading_type))
    else:
        cur.execute(base + ' ORDER BY COALESCE(vh.recorded_at, vh.timestamp) DESC', (patient_id, user_id))
    vitals = cur.fetchall()
    cur.close()
    conn.close()
    return vitals


def get_latest_vitals(patient_id, user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(f'''
        SELECT DISTINCT ON (vh.reading_type)
               vh.reading_type, vh.value, vh.unit, vh.timestamp, vh.recorded_at, vh.source, vh.change_from_previous
        FROM vitals_history vh
        JOIN patients p ON vh.patient_id = p.id
        WHERE vh.patient_id = %s AND {_patient_visible_clause()}
        ORDER BY vh.reading_type, COALESCE(vh.recorded_at, vh.timestamp) DESC
    ''', (patient_id, user_id))
    vitals = cur.fetchall()
    cur.close()
    conn.close()
    return vitals


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

def get_doctor_agents(active_only=True):
    conn = get_db_connection()
    cur = conn.cursor()
    if active_only:
        cur.execute('SELECT * FROM doctor_agents WHERE is_active = TRUE ORDER BY id')
    else:
        cur.execute('SELECT * FROM doctor_agents ORDER BY id')
    agents = cur.fetchall()
    cur.close()
    conn.close()
    return agents


def update_doctor_agent(agent_type, role=None, goal=None, backstory=None, is_active=None):
    conn = get_db_connection()
    cur = conn.cursor()
    updates = []
    values: list[Any] = []
    if role:
        updates.append("role = %s")
        values.append(role)
    if goal:
        updates.append("goal = %s")
        values.append(goal)
    if backstory:
        updates.append("backstory = %s")
        values.append(backstory)
    if is_active is not None:
        updates.append("is_active = %s")
        values.append(bool(is_active))
    if updates:
        updates.append("updated_at = CURRENT_TIMESTAMP")
        values.append(agent_type)
        cur.execute(f"UPDATE doctor_agents SET {', '.join(updates)} WHERE agent_type = %s", values)
        conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# Doctor memory
# ---------------------------------------------------------------------------

def save_doctor_memory(patient_id, user_id, doctor_type, memory_type, content, importance=5, source_file_id=None):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        _assert_patient_access(cur, patient_id, user_id)
        cur.execute('''
            INSERT INTO doctor_memory (patient_id, doctor_type, memory_type, content, importance, source_file_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (patient_id, doctor_type, memory_type, content, importance, source_file_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_doctor_memories(patient_id, user_id, doctor_type=None):
    conn = get_db_connection()
    cur = conn.cursor()
    base = f'''
        SELECT dm.* FROM doctor_memory dm
        JOIN patients p ON dm.patient_id = p.id
        WHERE dm.patient_id = %s AND {_patient_visible_clause()}
    '''
    if doctor_type:
        cur.execute(base + ' AND dm.doctor_type = %s ORDER BY dm.importance DESC, dm.created_at DESC',
                    (patient_id, user_id, doctor_type))
    else:
        cur.execute(base + ' ORDER BY dm.importance DESC, dm.created_at DESC', (patient_id, user_id))
    memories = cur.fetchall()
    cur.close()
    conn.close()
    return memories


# ---------------------------------------------------------------------------
# Conversations and chat
# ---------------------------------------------------------------------------

def get_user_conversations(user_id, patient_id=None):
    conn = get_db_connection()
    cur = conn.cursor()
    if patient_id:
        cur.execute('''
            SELECT c.*, p.name as patient_name
            FROM conversations c
            LEFT JOIN patients p ON c.patient_id = p.id
            WHERE c.user_id = %s AND c.patient_id = %s
            ORDER BY c.updated_at DESC
        ''', (user_id, patient_id))
    else:
        cur.execute('''
            SELECT c.*, p.name as patient_name
            FROM conversations c
            LEFT JOIN patients p ON c.patient_id = p.id
            WHERE c.user_id = %s
            ORDER BY c.updated_at DESC
        ''', (user_id,))
    conversations = cur.fetchall()
    cur.close()
    conn.close()
    return conversations


def create_conversation(user_id, patient_id, title='New Conversation'):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        _assert_patient_access(cur, patient_id, user_id)
        cur.execute('''
            INSERT INTO conversations (user_id, patient_id, title)
            VALUES (%s, %s, %s)
            RETURNING id
        ''', (user_id, patient_id, title))
        conv_id = cur.fetchone()['id']
        conn.commit()
        return conv_id
    finally:
        cur.close()
        conn.close()


def get_conversation(conversation_id, user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM conversations WHERE id = %s AND user_id = %s', (conversation_id, user_id))
    conv = cur.fetchone()
    cur.close()
    conn.close()
    return conv


def update_conversation_title(conversation_id, user_id, title):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        _assert_conversation_access(cur, conversation_id, user_id)
        cur.execute('UPDATE conversations SET title = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s',
                    (title, conversation_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def delete_conversation(conversation_id, user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        _assert_conversation_access(cur, conversation_id, user_id)
        cur.execute('DELETE FROM conversations WHERE id = %s', (conversation_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def save_chat_message_to_conversation(patient_id, conversation_id, user_id, role, content,
                                      addressed_to=None, responding_doctor=None, metadata=None):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        _assert_conversation_access(cur, conversation_id, user_id)
        _assert_patient_access(cur, patient_id, user_id)
        cur.execute('''
            INSERT INTO chat_history (patient_id, conversation_id, role, content, addressed_to, responding_doctor, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (patient_id, conversation_id, role, content, addressed_to, responding_doctor, json_dumps_safe(metadata)))
        msg_id = cur.fetchone()['id']
        cur.execute('UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s', (conversation_id,))
        conn.commit()
        return msg_id
    finally:
        cur.close()
        conn.close()


def get_conversation_history(conversation_id, user_id, limit=100):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT ch.* FROM chat_history ch
        JOIN conversations c ON ch.conversation_id = c.id
        WHERE ch.conversation_id = %s AND c.user_id = %s
        ORDER BY ch.timestamp ASC
        LIMIT %s
    ''', (conversation_id, user_id, limit))
    history = cur.fetchall()
    cur.close()
    conn.close()
    return history
