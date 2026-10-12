"""Postgres access layer.

Every read or write that touches patient data takes a user_id and checks that
the user may see that patient. Functions raise PermissionError when the check
fails so callers cannot silently operate on someone else's record.

Connections come from a process-wide psycopg2 ThreadedConnectionPool.
Streamlit runs each browser session's script in its own thread, so the pool
must be the threaded variant. Use db_cursor() for every query.

Schema is managed by Alembic (alembic/versions). run_migrations() brings a
database to the current revision at startup.

A0 note: orphan patients (user_id IS NULL) are still visible to any signed-in
user. Removing that and adding the leakage test is item 2.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor

from utils import json_dumps_safe

SPECIALIST_ORDER = ["neurologist", "cardiologist", "nephrologist", "endocrinologist", "radiologist"]

POOL_MIN = 1
POOL_MAX = 8
# Seconds a caller waits for a free connection before giving up.
POOL_WAIT_SECONDS = 30

_pool: pg_pool.ThreadedConnectionPool | None = None
_pool_url: str | None = None
_pool_lock = threading.Lock()
# psycopg2's pool raises PoolError as soon as maxconn is reached. This
# semaphore makes callers wait for a free slot instead.
_slots = threading.BoundedSemaphore(POOL_MAX)


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def _get_pool() -> pg_pool.ThreadedConnectionPool:
    global _pool, _pool_url
    url = database_url()
    with _pool_lock:
        if _pool is None or _pool.closed or _pool_url != url:
            if _pool is not None and not _pool.closed:
                _pool.closeall()
            _pool = pg_pool.ThreadedConnectionPool(POOL_MIN, POOL_MAX, url, cursor_factory=RealDictCursor)
            _pool_url = url
        return _pool


def close_pool() -> None:
    """Close every pooled connection. Used by tests and at shutdown."""
    global _pool
    with _pool_lock:
        if _pool is not None and not _pool.closed:
            _pool.closeall()
        _pool = None


@contextmanager
def db_cursor(commit: bool = False) -> Iterator[Any]:
    """Yield a RealDictCursor on a pooled connection.

    Commits on clean exit when commit=True, rolls back on any exception, and
    always returns the connection to the pool. A connection that failed at
    the socket level is discarded so the next call gets a fresh one.
    """
    if not _slots.acquire(timeout=POOL_WAIT_SECONDS):
        raise RuntimeError(f"no database connection became free within {POOL_WAIT_SECONDS}s")
    try:
        p = _get_pool()
        conn = p.getconn()
        broken = False
        try:
            if conn.closed:
                p.putconn(conn, close=True)
                conn = p.getconn()
            cur = conn.cursor()
            try:
                yield cur
                if commit:
                    conn.commit()
                else:
                    conn.rollback()
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                broken = True
                raise
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    broken = True
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
        finally:
            p.putconn(conn, close=broken)
    finally:
        _slots.release()


def get_db_connection():
    """Compatibility shim. Prefer db_cursor(). Caller must close() the connection."""
    return psycopg2.connect(database_url(), cursor_factory=RealDictCursor)


# ---------------------------------------------------------------------------
# Schema: Alembic
# ---------------------------------------------------------------------------

_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
PRE_ALEMBIC_REVISION = "0001"


def _alembic_config():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(_REPO_DIR, "alembic"))
    # ConfigParser interpolation: a literal percent sign must be doubled.
    cfg.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))
    return cfg


def migration_plan() -> dict[str, Any]:
    """Describe what run_migrations() will do, without doing it."""
    with db_cursor() as cur:
        cur.execute("SELECT to_regclass('public.alembic_version') AS v, to_regclass('public.patients') AS p")
        row = cur.fetchone()
        has_version_table = row["v"] is not None
        has_patients = row["p"] is not None
        current = None
        if has_version_table:
            cur.execute("SELECT version_num FROM alembic_version")
            r = cur.fetchone()
            current = r["version_num"] if r else None
    needs_stamp = (not has_version_table) and has_patients
    return {
        "has_version_table": has_version_table,
        "has_patients": has_patients,
        "current_revision": current,
        "will_stamp": PRE_ALEMBIC_REVISION if needs_stamp else None,
        "will_upgrade_to": "head",
    }


def run_migrations() -> dict[str, Any]:
    """Bring the database to the current Alembic head.

    A database created before Alembic (tables exist, no alembic_version) is
    stamped at PRE_ALEMBIC_REVISION first so revision 0001 is not re-run
    against existing tables. Later revisions then apply normally.
    Safe to call on every startup; a database already at head is a no-op.
    """
    from alembic import command

    plan = migration_plan()
    cfg = _alembic_config()
    if plan["will_stamp"]:
        command.stamp(cfg, plan["will_stamp"])
    command.upgrade(cfg, "head")
    close_pool()
    return plan


def init_database() -> None:
    """Kept for callers that still use the old name."""
    run_migrations()


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
    with db_cursor() as cur:
        try:
            _assert_patient_access(cur, patient_id, user_id)
            return True
        except PermissionError:
            return False


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_or_create_user(user_id, email=None, first_name=None, last_name=None, profile_image_url=None):
    with db_cursor(commit=True) as cur:
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
        return user


def get_user(user_id):
    with db_cursor() as cur:
        cur.execute('SELECT * FROM users WHERE id = %s', (user_id,))
        return cur.fetchone()


def migrate_orphan_patients_to_user(user_id):
    """Assign patients with no owner to this user. Removed in item 2."""
    with db_cursor(commit=True) as cur:
        cur.execute('UPDATE patients SET user_id = %s WHERE user_id IS NULL', (user_id,))
        return cur.rowcount


# ---------------------------------------------------------------------------
# Patients
# ---------------------------------------------------------------------------

def get_all_patients_for_user_or_orphans(user_id):
    with db_cursor() as cur:
        cur.execute('SELECT * FROM patients WHERE user_id = %s OR user_id IS NULL ORDER BY updated_at DESC', (user_id,))
        return cur.fetchall()


def get_user_patients(user_id):
    with db_cursor() as cur:
        cur.execute('SELECT * FROM patients WHERE user_id = %s ORDER BY updated_at DESC', (user_id,))
        return cur.fetchall()


def get_patient(patient_id, user_id):
    with db_cursor() as cur:
        cur.execute(f'SELECT * FROM patients p WHERE p.id = %s AND {_patient_visible_clause()}', (patient_id, user_id))
        return cur.fetchone()


def create_patient_for_user(user_id, name, age=None, weight_lbs=None, medications=None,
                            allergies=None, medical_history=None, stroke_date=None):
    with db_cursor(commit=True) as cur:
        cur.execute('''
            INSERT INTO patients (user_id, name, age, weight_lbs, medications, allergies, medical_history, stroke_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (user_id, name, age, weight_lbs, medications, allergies, medical_history, stroke_date))
        return cur.fetchone()['id']


_PATIENT_EDITABLE = {'name', 'age', 'weight_lbs', 'medications', 'allergies', 'medical_history', 'stroke_date'}


def update_patient(patient_id, user_id, **kwargs):
    with db_cursor(commit=True) as cur:
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


def delete_patient(patient_id, user_id):
    with db_cursor(commit=True) as cur:
        _assert_patient_access(cur, patient_id, user_id)
        cur.execute('DELETE FROM patients WHERE id = %s', (patient_id,))


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def save_patient_file(patient_id, user_id, filename, file_type, file_data, extracted_text=None, extracted_values=None):
    with db_cursor(commit=True) as cur:
        _assert_patient_access(cur, patient_id, user_id)
        cur.execute('''
            INSERT INTO patient_files (patient_id, filename, file_type, file_data, extracted_text, extracted_values)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (patient_id, filename, file_type, psycopg2.Binary(file_data), extracted_text,
              json_dumps_safe(extracted_values)))
        return cur.fetchone()['id']


def get_patient_files(patient_id, user_id):
    with db_cursor() as cur:
        cur.execute(f'''
            SELECT pf.id, pf.filename, pf.file_type, pf.extracted_text, pf.extracted_values, pf.uploaded_at
            FROM patient_files pf
            JOIN patients p ON pf.patient_id = p.id
            WHERE pf.patient_id = %s AND {_patient_visible_clause()}
            ORDER BY pf.uploaded_at DESC
        ''', (patient_id, user_id))
        return cur.fetchall()


def get_file_data(file_id, user_id):
    with db_cursor() as cur:
        cur.execute(f'''
            SELECT pf.file_data, pf.filename, pf.file_type, pf.patient_id
            FROM patient_files pf
            JOIN patients p ON pf.patient_id = p.id
            WHERE pf.id = %s AND {_patient_visible_clause()}
        ''', (file_id, user_id))
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Vitals
# ---------------------------------------------------------------------------

def save_vital_reading(patient_id, user_id, reading_type, value, unit, source="Manual Entry",
                       notes="", raw_data=None, recorded_at=None, source_file_id=None):
    """Save one reading. recorded_at is when it was taken; None means unknown."""
    with db_cursor(commit=True) as cur:
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
        return cur.fetchone()['id']


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
    base = f'''
        SELECT vh.* FROM vitals_history vh
        JOIN patients p ON vh.patient_id = p.id
        WHERE vh.patient_id = %s AND {_patient_visible_clause()}
    '''
    with db_cursor() as cur:
        if reading_type:
            cur.execute(base + ' AND vh.reading_type = %s ORDER BY COALESCE(vh.recorded_at, vh.timestamp) DESC',
                        (patient_id, user_id, reading_type))
        else:
            cur.execute(base + ' ORDER BY COALESCE(vh.recorded_at, vh.timestamp) DESC', (patient_id, user_id))
        return cur.fetchall()


def get_latest_vitals(patient_id, user_id):
    with db_cursor() as cur:
        cur.execute(f'''
            SELECT DISTINCT ON (vh.reading_type)
                   vh.reading_type, vh.value, vh.unit, vh.timestamp, vh.recorded_at, vh.source, vh.change_from_previous
            FROM vitals_history vh
            JOIN patients p ON vh.patient_id = p.id
            WHERE vh.patient_id = %s AND {_patient_visible_clause()}
            ORDER BY vh.reading_type, COALESCE(vh.recorded_at, vh.timestamp) DESC
        ''', (patient_id, user_id))
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

def get_doctor_agents(active_only=True):
    with db_cursor() as cur:
        if active_only:
            cur.execute('SELECT * FROM doctor_agents WHERE is_active = TRUE ORDER BY id')
        else:
            cur.execute('SELECT * FROM doctor_agents ORDER BY id')
        return cur.fetchall()


def update_doctor_agent(agent_type, role=None, goal=None, backstory=None, is_active=None):
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
    if not updates:
        return
    updates.append("updated_at = CURRENT_TIMESTAMP")
    values.append(agent_type)
    with db_cursor(commit=True) as cur:
        cur.execute(f"UPDATE doctor_agents SET {', '.join(updates)} WHERE agent_type = %s", values)


# ---------------------------------------------------------------------------
# Doctor memory
# ---------------------------------------------------------------------------

def save_doctor_memory(patient_id, user_id, doctor_type, memory_type, content, importance=5, source_file_id=None):
    with db_cursor(commit=True) as cur:
        _assert_patient_access(cur, patient_id, user_id)
        cur.execute('''
            INSERT INTO doctor_memory (patient_id, doctor_type, memory_type, content, importance, source_file_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (patient_id, doctor_type, memory_type, content, importance, source_file_id))


def get_doctor_memories(patient_id, user_id, doctor_type=None):
    base = f'''
        SELECT dm.* FROM doctor_memory dm
        JOIN patients p ON dm.patient_id = p.id
        WHERE dm.patient_id = %s AND {_patient_visible_clause()}
    '''
    with db_cursor() as cur:
        if doctor_type:
            cur.execute(base + ' AND dm.doctor_type = %s ORDER BY dm.importance DESC, dm.created_at DESC',
                        (patient_id, user_id, doctor_type))
        else:
            cur.execute(base + ' ORDER BY dm.importance DESC, dm.created_at DESC', (patient_id, user_id))
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Conversations and chat
# ---------------------------------------------------------------------------

def get_user_conversations(user_id, patient_id=None):
    with db_cursor() as cur:
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
        return cur.fetchall()


def create_conversation(user_id, patient_id, title='New Conversation'):
    with db_cursor(commit=True) as cur:
        _assert_patient_access(cur, patient_id, user_id)
        cur.execute('''
            INSERT INTO conversations (user_id, patient_id, title)
            VALUES (%s, %s, %s)
            RETURNING id
        ''', (user_id, patient_id, title))
        return cur.fetchone()['id']


def get_conversation(conversation_id, user_id):
    with db_cursor() as cur:
        cur.execute('SELECT * FROM conversations WHERE id = %s AND user_id = %s', (conversation_id, user_id))
        return cur.fetchone()


def update_conversation_title(conversation_id, user_id, title):
    with db_cursor(commit=True) as cur:
        _assert_conversation_access(cur, conversation_id, user_id)
        cur.execute('UPDATE conversations SET title = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s',
                    (title, conversation_id))


def delete_conversation(conversation_id, user_id):
    with db_cursor(commit=True) as cur:
        _assert_conversation_access(cur, conversation_id, user_id)
        cur.execute('DELETE FROM conversations WHERE id = %s', (conversation_id,))


def save_chat_message_to_conversation(patient_id, conversation_id, user_id, role, content,
                                      addressed_to=None, responding_doctor=None, metadata=None):
    with db_cursor(commit=True) as cur:
        _assert_conversation_access(cur, conversation_id, user_id)
        _assert_patient_access(cur, patient_id, user_id)
        cur.execute('''
            INSERT INTO chat_history (patient_id, conversation_id, role, content, addressed_to, responding_doctor, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (patient_id, conversation_id, role, content, addressed_to, responding_doctor, json_dumps_safe(metadata)))
        msg_id = cur.fetchone()['id']
        cur.execute('UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s', (conversation_id,))
        return msg_id


def get_conversation_history(conversation_id, user_id, limit=100):
    with db_cursor() as cur:
        cur.execute('''
            SELECT ch.* FROM chat_history ch
            JOIN conversations c ON ch.conversation_id = c.id
            WHERE ch.conversation_id = %s AND c.user_id = %s
            ORDER BY ch.timestamp ASC
            LIMIT %s
        ''', (conversation_id, user_id, limit))
        return cur.fetchall()
