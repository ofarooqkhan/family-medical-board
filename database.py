"""Postgres access layer: pool, migrations, access checks, audit, and the core
tables (users, patients, documents, vitals, agents, conversations).

Access model: this deployment serves one family. Every active user can see
every patient. Roles decide who may write:
  viewer  read only
  editor  change patient records
  admin   editor plus user, invite, and agent management
Every query is scoped by patient_id. Writes raise PermissionError when the
role is too low, and every write adds an audit_log row.

Connections come from a process-wide psycopg2 ThreadedConnectionPool
(Streamlit runs each session in its own thread). Use db_cursor() for every
query. Schema is managed by Alembic; run_migrations() runs at startup.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor

from utils import json_dumps_safe

ROLE_RANK = {"viewer": 1, "editor": 2, "admin": 3}

POOL_MIN = 1
POOL_MAX = 8
POOL_WAIT_SECONDS = 30

_pool: pg_pool.ThreadedConnectionPool | None = None
_pool_url: str | None = None
_pool_lock = threading.Lock()
_slots = threading.BoundedSemaphore(POOL_MAX)


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


# TCP keepalives so hosted Postgres (Neon behind Replit, Render) does not drop
# idle SSL sessions under us between two page loads.
_CONNECT_KWARGS = dict(cursor_factory=RealDictCursor, keepalives=1, keepalives_idle=30,
                       keepalives_interval=10, keepalives_count=3, connect_timeout=15)


def _get_pool() -> pg_pool.ThreadedConnectionPool:
    global _pool, _pool_url
    url = database_url()
    with _pool_lock:
        if _pool is None or _pool.closed or _pool_url != url:
            if _pool is not None and not _pool.closed:
                _pool.closeall()
            last_error = None
            for attempt in range(3):  # a sleeping hosted database can refuse the first connect
                try:
                    _pool = pg_pool.ThreadedConnectionPool(POOL_MIN, POOL_MAX, url, **_CONNECT_KWARGS)
                    break
                except psycopg2.OperationalError as e:
                    last_error = e
                    time.sleep(1.5 * (attempt + 1))
            else:
                raise last_error  # type: ignore[misc]
            _pool_url = url
        return _pool


def _checkout(p: pg_pool.ThreadedConnectionPool):
    """Get a pooled connection that is known to be alive.

    A connection that sat idle may have been closed by the server without the
    client noticing (the first query then fails with "SSL connection has been
    closed unexpectedly"). Ping it first; replace it once if the ping fails.
    """
    last_error = None
    for attempt in range(2):
        conn = p.getconn()
        try:
            if conn.closed:
                raise psycopg2.InterfaceError("connection already closed")
            with conn.cursor() as ping:
                ping.execute("SELECT 1")
            conn.rollback()
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            last_error = e
            p.putconn(conn, close=True)
    raise last_error  # type: ignore[misc]


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None and not _pool.closed:
            _pool.closeall()
        _pool = None


@contextmanager
def db_cursor(commit: bool = False) -> Iterator[Any]:
    """Yield a RealDictCursor on a pooled connection; commit or roll back; return it."""
    if not _slots.acquire(timeout=POOL_WAIT_SECONDS):
        raise RuntimeError(f"no database connection became free within {POOL_WAIT_SECONDS}s")
    try:
        p = _get_pool()
        conn = _checkout(p)
        broken = False
        try:
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


# ---------------------------------------------------------------------------
# Schema: Alembic
# ---------------------------------------------------------------------------

_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
PRE_ALEMBIC_REVISION = "0001"


def _alembic_config():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(_REPO_DIR, "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))
    return cfg


def migration_plan() -> dict[str, Any]:
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
    """Bring the database to head. A pre-Alembic database is stamped at 0001 first."""
    from alembic import command

    plan = migration_plan()
    cfg = _alembic_config()
    if plan["will_stamp"]:
        command.stamp(cfg, plan["will_stamp"])
    command.upgrade(cfg, "head")
    close_pool()
    return plan


def init_database() -> None:
    run_migrations()


# ---------------------------------------------------------------------------
# Access checks and audit
# ---------------------------------------------------------------------------

def require_role(user: dict | None, role: str) -> None:
    """Raise PermissionError unless user is active and holds at least `role`."""
    if not user or not user.get("id"):
        raise PermissionError("sign in required")
    if user.get("is_active") is False:
        raise PermissionError("account is disabled")
    if ROLE_RANK.get(user.get("role", "viewer"), 0) < ROLE_RANK[role]:
        raise PermissionError(f"{role} role required")


def _assert_patient(cur, patient_id: int) -> dict:
    cur.execute("SELECT * FROM patients WHERE id = %s", (patient_id,))
    row = cur.fetchone()
    if row is None:
        raise PermissionError(f"no patient {patient_id}")
    return row


def audit(cur, user: dict | None, patient_id: int | None, action: str, entity: str | None = None,
          entity_id: int | None = None, details: Any = None) -> None:
    cur.execute(
        "INSERT INTO audit_log (user_id, patient_id, action, entity, entity_id, details) VALUES (%s, %s, %s, %s, %s, %s)",
        ((user or {}).get("id"), patient_id, action, entity, entity_id, json_dumps_safe(details)),
    )


def get_audit_log(patient_id: int | None = None, limit: int = 200):
    with db_cursor() as cur:
        if patient_id is None:
            cur.execute("""SELECT a.*, u.display_name FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
                           ORDER BY a.created_at DESC LIMIT %s""", (limit,))
        else:
            cur.execute("""SELECT a.*, u.display_name FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
                           WHERE a.patient_id = %s ORDER BY a.created_at DESC LIMIT %s""", (patient_id, limit))
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Users (account creation and passwords live in auth.py)
# ---------------------------------------------------------------------------

def get_user(user_id: str):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()


def get_user_by_email(email: str):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE lower(email) = lower(%s)", (email.strip(),))
        return cur.fetchone()


def list_users():
    with db_cursor() as cur:
        cur.execute("SELECT id, email, display_name, role, language, is_active, created_at, last_login_at FROM users ORDER BY created_at")
        return cur.fetchall()


def count_users() -> int:
    with db_cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM users")
        return cur.fetchone()["n"]


def update_user_settings(user: dict, *, display_name: str | None = None, language: str | None = None) -> None:
    require_role(user, "viewer")
    with db_cursor(commit=True) as cur:
        if display_name is not None:
            cur.execute("UPDATE users SET display_name = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (display_name, user["id"]))
        if language is not None:
            cur.execute("UPDATE users SET language = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (language, user["id"]))
        audit(cur, user, None, "user.settings", "user", None, {"display_name": display_name, "language": language})


def set_user_role(admin: dict, target_user_id: str, role: str) -> None:
    require_role(admin, "admin")
    if role not in ROLE_RANK:
        raise ValueError(f"unknown role {role}")
    with db_cursor(commit=True) as cur:
        cur.execute("UPDATE users SET role = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (role, target_user_id))
        audit(cur, admin, None, "user.role", "user", None, {"user_id": target_user_id, "role": role})


def set_user_active(admin: dict, target_user_id: str, is_active: bool) -> None:
    require_role(admin, "admin")
    if target_user_id == admin["id"] and not is_active:
        raise ValueError("you cannot disable your own account")
    with db_cursor(commit=True) as cur:
        cur.execute("UPDATE users SET is_active = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (is_active, target_user_id))
        audit(cur, admin, None, "user.active", "user", None, {"user_id": target_user_id, "is_active": is_active})


# ---------------------------------------------------------------------------
# Patients
# ---------------------------------------------------------------------------

PATIENT_FIELDS = ("name", "age", "date_of_birth", "sex", "weight_lbs", "medications", "allergies",
                  "medical_history", "stroke_date", "primary_doctor", "notes")


def list_patients(user: dict, include_archived: bool = False):
    require_role(user, "viewer")
    with db_cursor() as cur:
        if include_archived:
            cur.execute("SELECT * FROM patients ORDER BY name")
        else:
            cur.execute("SELECT * FROM patients WHERE NOT is_archived ORDER BY name")
        return cur.fetchall()


def get_patient(patient_id: int, user: dict):
    require_role(user, "viewer")
    with db_cursor() as cur:
        cur.execute("SELECT * FROM patients WHERE id = %s", (patient_id,))
        return cur.fetchone()


def create_patient(user: dict, name: str, **fields) -> int:
    require_role(user, "editor")
    cols = ["user_id", "name"]
    vals: list[Any] = [user["id"], name]
    for k, v in fields.items():
        if k in PATIENT_FIELDS and k != "name":
            cols.append(k)
            vals.append(v)
    with db_cursor(commit=True) as cur:
        cur.execute(f"INSERT INTO patients ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(vals))}) RETURNING id", vals)
        pid = cur.fetchone()["id"]
        audit(cur, user, pid, "patient.create", "patient", pid, {"name": name})
        return pid


def update_patient(patient_id: int, user: dict, **fields) -> None:
    require_role(user, "editor")
    sets, vals = [], []
    for k, v in fields.items():
        if k in PATIENT_FIELDS:
            sets.append(f"{k} = %s")
            vals.append(v)
    if not sets:
        return
    with db_cursor(commit=True) as cur:
        _assert_patient(cur, patient_id)
        vals.append(patient_id)
        cur.execute(f"UPDATE patients SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE id = %s", vals)
        audit(cur, user, patient_id, "patient.update", "patient", patient_id, {k: str(v) for k, v in fields.items() if k in PATIENT_FIELDS})


def archive_patient(patient_id: int, user: dict, archived: bool = True) -> None:
    require_role(user, "admin")
    with db_cursor(commit=True) as cur:
        _assert_patient(cur, patient_id)
        cur.execute("UPDATE patients SET is_archived = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (archived, patient_id))
        audit(cur, user, patient_id, "patient.archive" if archived else "patient.unarchive", "patient", patient_id)


def delete_patient(patient_id: int, user: dict) -> None:
    require_role(user, "admin")
    with db_cursor(commit=True) as cur:
        row = _assert_patient(cur, patient_id)
        audit(cur, user, None, "patient.delete", "patient", patient_id, {"name": row["name"]})
        cur.execute("DELETE FROM patients WHERE id = %s", (patient_id,))


# ---------------------------------------------------------------------------
# Documents (patient_files) and pages
# ---------------------------------------------------------------------------

def save_document(patient_id: int, user: dict, filename: str, mime_type: str, data: bytes,
                  document_kind: str | None = None, pages: list[dict] | None = None,
                  extracted_text: str | None = None) -> int:
    """Store a file and its rendered pages. pages: [{page_number, image_png, width, height, text}]."""
    require_role(user, "editor")
    digest = hashlib.sha256(data).hexdigest()
    with db_cursor(commit=True) as cur:
        _assert_patient(cur, patient_id)
        cur.execute("""
            INSERT INTO patient_files (patient_id, filename, file_type, file_data, extracted_text, mime_type, sha256,
                                       page_count, uploaded_by, document_kind, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'uploaded') RETURNING id
        """, (patient_id, filename, document_kind, psycopg2.Binary(data), extracted_text, mime_type, digest,
              len(pages or []), user["id"], document_kind))
        file_id = cur.fetchone()["id"]
        for p in pages or []:
            cur.execute("""
                INSERT INTO document_pages (file_id, page_number, image_png, width, height, text)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (file_id, p["page_number"], psycopg2.Binary(p["image_png"]) if p.get("image_png") else None,
                  p.get("width"), p.get("height"), p.get("text")))
        audit(cur, user, patient_id, "document.upload", "document", file_id, {"filename": filename, "pages": len(pages or [])})
        return file_id


def list_documents(patient_id: int, user: dict):
    require_role(user, "viewer")
    with db_cursor() as cur:
        cur.execute("""
            SELECT id, patient_id, filename, file_type, mime_type, page_count, document_kind, document_date, status,
                   extraction_model, extraction_error, uploaded_by, uploaded_at
            FROM patient_files WHERE patient_id = %s ORDER BY uploaded_at DESC
        """, (patient_id,))
        return cur.fetchall()


def get_document(file_id: int, patient_id: int, user: dict, with_data: bool = False):
    require_role(user, "viewer")
    cols = "*" if with_data else ("id, patient_id, filename, file_type, mime_type, sha256, page_count, document_kind, document_date, "
                                  "status, extraction, extraction_model, extraction_error, extracted_text, uploaded_by, uploaded_at")
    with db_cursor() as cur:
        cur.execute(f"SELECT {cols} FROM patient_files WHERE id = %s AND patient_id = %s", (file_id, patient_id))
        return cur.fetchone()


def get_document_pages(file_id: int, patient_id: int, user: dict, with_images: bool = True):
    require_role(user, "viewer")
    cols = "dp.id, dp.file_id, dp.page_number, dp.width, dp.height, dp.text" + (", dp.image_png" if with_images else "")
    with db_cursor() as cur:
        cur.execute(f"""
            SELECT {cols} FROM document_pages dp JOIN patient_files pf ON pf.id = dp.file_id
            WHERE dp.file_id = %s AND pf.patient_id = %s ORDER BY dp.page_number
        """, (file_id, patient_id))
        return cur.fetchall()


def set_document_extraction(file_id: int, patient_id: int, user: dict, *, extraction: Any, model: str | None,
                            document_kind: str | None, document_date=None, error: str | None = None,
                            extracted_text: str | None = None) -> None:
    require_role(user, "editor")
    with db_cursor(commit=True) as cur:
        cur.execute("""
            UPDATE patient_files SET extraction = %s, extraction_model = %s, document_kind = COALESCE(%s, document_kind),
                   document_date = COALESCE(%s, document_date), extraction_error = %s,
                   extracted_text = COALESCE(%s, extracted_text),
                   status = CASE WHEN %s IS NULL THEN 'extracted' ELSE 'failed' END
            WHERE id = %s AND patient_id = %s
        """, (json_dumps_safe(extraction), model, document_kind, document_date, error, extracted_text, error, file_id, patient_id))
        audit(cur, user, patient_id, "document.extract", "document", file_id, {"model": model, "error": error})


def set_document_status(file_id: int, patient_id: int, user: dict, status: str) -> None:
    require_role(user, "editor")
    with db_cursor(commit=True) as cur:
        cur.execute("UPDATE patient_files SET status = %s WHERE id = %s AND patient_id = %s", (status, file_id, patient_id))


def delete_document(file_id: int, patient_id: int, user: dict) -> None:
    require_role(user, "editor")
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM patient_files WHERE id = %s AND patient_id = %s", (file_id, patient_id))
        audit(cur, user, patient_id, "document.delete", "document", file_id)


# ---------------------------------------------------------------------------
# Extracted items (pending review)
# ---------------------------------------------------------------------------

def add_extracted_items(patient_id: int, file_id: int, user: dict, items: list[dict]) -> list[int]:
    """items: [{page_number, item_type, payload, confidence}]. Status starts as pending."""
    require_role(user, "editor")
    ids = []
    with db_cursor(commit=True) as cur:
        for it in items:
            cur.execute("""
                INSERT INTO extracted_items (patient_id, file_id, page_number, item_type, payload, confidence)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """, (patient_id, file_id, it.get("page_number"), it["item_type"], json_dumps_safe(it["payload"]), it.get("confidence")))
            ids.append(cur.fetchone()["id"])
        audit(cur, user, patient_id, "document.items", "document", file_id, {"count": len(items)})
    return ids


def list_extracted_items(patient_id: int, user: dict, file_id: int | None = None, status: str | None = None):
    require_role(user, "viewer")
    sql = "SELECT * FROM extracted_items WHERE patient_id = %s"
    params: list[Any] = [patient_id]
    if file_id is not None:
        sql += " AND file_id = %s"
        params.append(file_id)
    if status:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY file_id, page_number, id"
    with db_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def set_extracted_item_status(item_id: int, patient_id: int, user: dict, status: str, payload: Any = None) -> None:
    require_role(user, "editor")
    with db_cursor(commit=True) as cur:
        if payload is not None:
            cur.execute("""UPDATE extracted_items SET status = %s, payload = %s, reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP
                           WHERE id = %s AND patient_id = %s""", (status, json_dumps_safe(payload), user["id"], item_id, patient_id))
        else:
            cur.execute("""UPDATE extracted_items SET status = %s, reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP
                           WHERE id = %s AND patient_id = %s""", (status, user["id"], item_id, patient_id))
        audit(cur, user, patient_id, f"item.{status}", "extracted_item", item_id)


# ---------------------------------------------------------------------------
# Vitals and lab values
# ---------------------------------------------------------------------------

def save_vital_reading(patient_id: int, user: dict, reading_type: str, value: float, unit: str | None,
                       source: str = "Manual Entry", notes: str = "", raw_data: Any = None, recorded_at=None,
                       source_file_id: int | None = None, source_page: int | None = None,
                       extracted_item_id: int | None = None, reference_range: str | None = None,
                       flag: str | None = None) -> int:
    require_role(user, "editor")
    with db_cursor(commit=True) as cur:
        _assert_patient(cur, patient_id)
        cur.execute("""SELECT value FROM vitals_history WHERE patient_id = %s AND reading_type = %s
                       ORDER BY COALESCE(recorded_at, timestamp) DESC LIMIT 1""", (patient_id, reading_type))
        prev = cur.fetchone()
        previous_value = prev["value"] if prev else None
        change = value - previous_value if previous_value is not None else None
        cur.execute("""
            INSERT INTO vitals_history (patient_id, reading_type, value, unit, source, notes, raw_data, previous_value,
                                        change_from_previous, recorded_at, source_file_id, source_page, extracted_item_id,
                                        reference_range, flag, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (patient_id, reading_type, value, unit, source, notes, json_dumps_safe(raw_data), previous_value, change,
              recorded_at, source_file_id, source_page, extracted_item_id, reference_range, flag, user["id"]))
        row_id = cur.fetchone()["id"]
        audit(cur, user, patient_id, "vital.create", "vital", row_id, {"reading_type": reading_type, "value": value})
        return row_id


def get_vitals_history(patient_id: int, user: dict, reading_type: str | None = None, limit: int = 500):
    require_role(user, "viewer")
    with db_cursor() as cur:
        if reading_type:
            cur.execute("""SELECT * FROM vitals_history WHERE patient_id = %s AND reading_type = %s
                           ORDER BY COALESCE(recorded_at, timestamp) DESC LIMIT %s""", (patient_id, reading_type, limit))
        else:
            cur.execute("""SELECT * FROM vitals_history WHERE patient_id = %s
                           ORDER BY COALESCE(recorded_at, timestamp) DESC LIMIT %s""", (patient_id, limit))
        return cur.fetchall()


def get_latest_vitals(patient_id: int, user: dict):
    require_role(user, "viewer")
    with db_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (reading_type) reading_type, value, unit, timestamp, recorded_at, source,
                   change_from_previous, reference_range, flag, source_file_id, source_page
            FROM vitals_history WHERE patient_id = %s
            ORDER BY reading_type, COALESCE(recorded_at, timestamp) DESC
        """, (patient_id,))
        return cur.fetchall()


def delete_vital_reading(reading_id: int, patient_id: int, user: dict) -> None:
    require_role(user, "editor")
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM vitals_history WHERE id = %s AND patient_id = %s", (reading_id, patient_id))
        audit(cur, user, patient_id, "vital.delete", "vital", reading_id)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

def get_doctor_agents(active_only: bool = True):
    with db_cursor() as cur:
        if active_only:
            cur.execute("SELECT * FROM doctor_agents WHERE is_active = TRUE ORDER BY sort_order, id")
        else:
            cur.execute("SELECT * FROM doctor_agents ORDER BY sort_order, id")
        return cur.fetchall()


def update_doctor_agent(user: dict, agent_type: str, **fields) -> None:
    require_role(user, "admin")
    allowed = {"display_name", "role", "goal", "backstory", "system_prompt", "is_active", "provider", "model", "domains"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = %s")
            vals.append(json_dumps_safe(v) if k == "domains" else v)
    if not sets:
        return
    vals.append(agent_type)
    with db_cursor(commit=True) as cur:
        cur.execute(f"UPDATE doctor_agents SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE agent_type = %s", vals)
        audit(cur, user, None, "agent.update", "agent", None, {"agent_type": agent_type, "fields": sorted(fields)})


# ---------------------------------------------------------------------------
# Doctor memory (findings the board stores for later runs)
# ---------------------------------------------------------------------------

def save_doctor_memory(patient_id: int, user: dict, doctor_type: str, memory_type: str, content: str,
                       importance: int = 5, source_file_id: int | None = None) -> None:
    require_role(user, "viewer")
    with db_cursor(commit=True) as cur:
        _assert_patient(cur, patient_id)
        cur.execute("""INSERT INTO doctor_memory (patient_id, doctor_type, memory_type, content, importance, source_file_id)
                       VALUES (%s, %s, %s, %s, %s, %s)""", (patient_id, doctor_type, memory_type, content, importance, source_file_id))


def get_doctor_memories(patient_id: int, user: dict, doctor_type: str | None = None, limit: int = 50):
    require_role(user, "viewer")
    with db_cursor() as cur:
        if doctor_type:
            cur.execute("""SELECT * FROM doctor_memory WHERE patient_id = %s AND doctor_type = %s
                           ORDER BY importance DESC, created_at DESC LIMIT %s""", (patient_id, doctor_type, limit))
        else:
            cur.execute("""SELECT * FROM doctor_memory WHERE patient_id = %s
                           ORDER BY importance DESC, created_at DESC LIMIT %s""", (patient_id, limit))
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Conversations and chat (per user, per patient)
# ---------------------------------------------------------------------------

def get_user_conversations(user: dict, patient_id: int | None = None):
    require_role(user, "viewer")
    with db_cursor() as cur:
        if patient_id:
            cur.execute("""SELECT c.*, p.name AS patient_name FROM conversations c LEFT JOIN patients p ON c.patient_id = p.id
                           WHERE c.user_id = %s AND c.patient_id = %s ORDER BY c.updated_at DESC""", (user["id"], patient_id))
        else:
            cur.execute("""SELECT c.*, p.name AS patient_name FROM conversations c LEFT JOIN patients p ON c.patient_id = p.id
                           WHERE c.user_id = %s ORDER BY c.updated_at DESC""", (user["id"],))
        return cur.fetchall()


def create_conversation(user: dict, patient_id: int, title: str = "New Conversation") -> int:
    require_role(user, "viewer")
    with db_cursor(commit=True) as cur:
        _assert_patient(cur, patient_id)
        cur.execute("INSERT INTO conversations (user_id, patient_id, title) VALUES (%s, %s, %s) RETURNING id",
                    (user["id"], patient_id, title))
        return cur.fetchone()["id"]


def _assert_conversation(cur, conversation_id: int, user: dict) -> dict:
    cur.execute("SELECT * FROM conversations WHERE id = %s AND user_id = %s", (conversation_id, user["id"]))
    conv = cur.fetchone()
    if conv is None:
        raise PermissionError(f"user {user['id']} may not access conversation {conversation_id}")
    return conv


def get_conversation(conversation_id: int, user: dict):
    require_role(user, "viewer")
    with db_cursor() as cur:
        cur.execute("SELECT * FROM conversations WHERE id = %s AND user_id = %s", (conversation_id, user["id"]))
        return cur.fetchone()


def update_conversation_title(conversation_id: int, user: dict, title: str) -> None:
    with db_cursor(commit=True) as cur:
        _assert_conversation(cur, conversation_id, user)
        cur.execute("UPDATE conversations SET title = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (title, conversation_id))


def delete_conversation(conversation_id: int, user: dict) -> None:
    with db_cursor(commit=True) as cur:
        _assert_conversation(cur, conversation_id, user)
        cur.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))


def save_chat_message(patient_id: int, conversation_id: int, user: dict, role: str, content: str,
                      addressed_to: str | None = None, responding_doctor: str | None = None, metadata: Any = None) -> int:
    with db_cursor(commit=True) as cur:
        conv = _assert_conversation(cur, conversation_id, user)
        if conv["patient_id"] != patient_id:
            raise PermissionError("conversation belongs to another patient")
        cur.execute("""INSERT INTO chat_history (patient_id, conversation_id, role, content, addressed_to, responding_doctor, metadata)
                       VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (patient_id, conversation_id, role, content, addressed_to, responding_doctor, json_dumps_safe(metadata)))
        msg_id = cur.fetchone()["id"]
        cur.execute("UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s", (conversation_id,))
        return msg_id


def get_conversation_history(conversation_id: int, user: dict, limit: int = 100):
    with db_cursor() as cur:
        cur.execute("""SELECT ch.* FROM chat_history ch JOIN conversations c ON ch.conversation_id = c.id
                       WHERE ch.conversation_id = %s AND c.user_id = %s ORDER BY ch.timestamp ASC LIMIT %s""",
                    (conversation_id, user["id"], limit))
        return cur.fetchall()
