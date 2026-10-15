"""Database and auth tests. Run only when TEST_DATABASE_URL points at a throwaway Postgres.

Every test starts from an empty public schema (fixtures db, migrated and
accounts live in tests/conftest.py). Nothing here touches the DATABASE_URL
the app uses.
"""
import os
import threading

import psycopg2
import pytest

TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")

# The eight tables as the pre-Alembic Replit app created them. Used to simulate
# the live database that existed before this repo.
LEGACY_DDL = """
CREATE TABLE users (id VARCHAR(255) PRIMARY KEY, email VARCHAR(255), first_name VARCHAR(255),
  last_name VARCHAR(255), profile_image_url TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE patients (id SERIAL PRIMARY KEY, user_id VARCHAR(255) REFERENCES users(id) ON DELETE CASCADE,
  name VARCHAR(255) NOT NULL, age INTEGER, weight_lbs REAL, medications TEXT, allergies TEXT,
  medical_history TEXT, stroke_date TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE conversations (id SERIAL PRIMARY KEY, user_id VARCHAR(255) REFERENCES users(id) ON DELETE CASCADE,
  patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE, title VARCHAR(255) DEFAULT 'New Conversation',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE patient_files (id SERIAL PRIMARY KEY, patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
  filename VARCHAR(255) NOT NULL, file_type VARCHAR(50), file_data BYTEA, extracted_text TEXT,
  extracted_values JSONB, uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE vitals_history (id SERIAL PRIMARY KEY, patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, reading_type VARCHAR(100) NOT NULL, value REAL, unit VARCHAR(50),
  source VARCHAR(255), notes TEXT, raw_data JSONB, previous_value REAL, change_from_previous REAL);
CREATE TABLE doctor_agents (id SERIAL PRIMARY KEY, agent_type VARCHAR(100) UNIQUE NOT NULL,
  display_name VARCHAR(255) NOT NULL, role VARCHAR(255) NOT NULL, goal TEXT NOT NULL, backstory TEXT NOT NULL,
  is_active BOOLEAN DEFAULT TRUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE doctor_memory (id SERIAL PRIMARY KEY, patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
  doctor_type VARCHAR(100) NOT NULL, memory_type VARCHAR(50) NOT NULL, content TEXT NOT NULL,
  importance INTEGER DEFAULT 5, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  source_file_id INTEGER REFERENCES patient_files(id) ON DELETE SET NULL);
CREATE TABLE chat_history (id SERIAL PRIMARY KEY, patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
  conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, role VARCHAR(50) NOT NULL, content TEXT NOT NULL,
  addressed_to VARCHAR(100), responding_doctor VARCHAR(100), metadata JSONB);
INSERT INTO doctor_agents (agent_type, display_name, role, goal, backstory)
  VALUES ('neurologist', 'Stroke Neurologist', 'Stroke Neurologist', 'adjust medications', 'old prompt');
"""

EXPECTED_TABLES = {
    "users", "patients", "conversations", "patient_files", "vitals_history", "doctor_agents", "doctor_memory",
    "chat_history", "alembic_version", "invites", "audit_log", "document_pages", "extracted_items", "medications",
    "medication_changes", "appointments", "board_runs", "sessions", "consultations",
}

HEAD = "0007"


def _raw():
    return psycopg2.connect(TEST_URL)


def _query(sql, params=None):
    conn = _raw()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def _columns(table):
    return [r[0] for r in _query(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s "
        "ORDER BY ordinal_position", (table,))]


def _tables():
    return {r[0] for r in _query("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}


# Migrations

def test_fresh_database_reaches_head(db):
    plan = db.run_migrations()
    assert plan["will_stamp"] is None
    assert _tables() == EXPECTED_TABLES
    assert db.migration_plan()["current_revision"] == HEAD
    agents = db.get_doctor_agents(active_only=False)
    assert len(agents) == 15
    assert all(a["system_prompt"] and "never give medical advice" in a["system_prompt"].lower() for a in agents)
    assert {a["agent_type"] for a in agents if not a["is_active"]} == {"pulmonologist", "geriatrician", "geriatric_psychiatry"}


def test_legacy_database_is_stamped_then_upgraded_and_keeps_rows(db):
    conn = _raw()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(LEGACY_DDL)
    cur.execute("INSERT INTO users (id, first_name) VALUES ('u1', 'Owner')")
    cur.execute("INSERT INTO patients (user_id, name) VALUES ('u1', 'Parent One'), (NULL, 'Orphan')")
    cur.execute("INSERT INTO vitals_history (patient_id, reading_type, value) VALUES (1, 'glucose', 120), (1, 'glucose', 130)")
    cur.close()
    conn.close()

    assert "recorded_at" not in _columns("vitals_history")
    plan = db.run_migrations()
    assert plan["will_stamp"] == "0001"
    assert {"recorded_at", "source_file_id", "source_page", "reference_range"} <= set(_columns("vitals_history"))
    assert db.migration_plan()["current_revision"] == HEAD
    assert _query("SELECT count(*) FROM users")[0][0] == 1
    assert _query("SELECT count(*) FROM patients")[0][0] == 2
    assert _query("SELECT count(*) FROM vitals_history")[0][0] == 2
    # The old advice-giving prompt was replaced, the row kept.
    goal, prompt = _query("SELECT goal, system_prompt FROM doctor_agents WHERE agent_type='neurologist'")[0]
    assert "adjust" not in goal.lower()
    assert "never give medical advice" in prompt.lower()


def test_legacy_duplicate_emails_are_resolved_before_the_unique_index(db):
    """The Replit dev login made one user row per sign-in with the same email."""
    conn = _raw()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(LEGACY_DDL)
    cur.execute("""INSERT INTO users (id, email, first_name, created_at) VALUES
                   ('u-old', 'Owner@Example.com', 'Owner', '2026-01-01'),
                   ('u-mid', 'owner@example.com', 'Owner', '2026-02-01'),
                   ('u-new', 'owner@example.com', 'Owner', '2026-03-01'),
                   ('u-other', 'other@example.com', 'Other', '2026-03-01')""")
    cur.execute("INSERT INTO patients (user_id, name) VALUES ('u-new', 'Parent One')")
    cur.close()
    conn.close()
    db.run_migrations()
    rows = dict(_query("SELECT id, email FROM users"))
    assert rows == {"u-old": "Owner@Example.com", "u-mid": None, "u-new": None, "u-other": "other@example.com"}
    assert _query("SELECT user_id FROM patients")[0][0] == "u-new"
    import auth
    assert auth.is_first_user()  # none of the legacy rows has a password, so the next signup is admin


def test_second_run_is_a_no_op(migrated):
    before = migrated.migration_plan()
    plan = migrated.run_migrations()
    assert plan["will_stamp"] is None
    assert migrated.migration_plan() == before


def test_downgrade_and_upgrade_round_trip(migrated):
    from alembic import command
    cfg = migrated._alembic_config()
    command.downgrade(cfg, "0002")
    migrated.close_pool()
    assert "medications" not in _tables()
    assert "role" not in _columns("users")
    command.upgrade(cfg, "head")
    migrated.close_pool()
    assert _tables() == EXPECTED_TABLES


# Auth

def test_first_signup_is_admin_then_invites_are_required(migrated):
    import auth
    assert auth.is_first_user()
    admin = auth.signup("a@example.com", "correct horse battery", "A")
    assert admin["role"] == "admin"
    assert not auth.is_first_user()
    with pytest.raises(auth.AuthError):
        auth.signup("b@example.com", "correct horse battery", "B")
    with pytest.raises(auth.AuthError):
        auth.signup("b@example.com", "correct horse battery", "B", "not-a-code")
    inv = auth.create_invite(admin, "editor")
    b = auth.signup("b@example.com", "correct horse battery", "B", inv["code"])
    assert b["role"] == "editor"
    with pytest.raises(auth.AuthError):
        auth.signup("c@example.com", "correct horse battery", "C", inv["code"])  # already used


def test_signup_claims_a_legacy_row_without_password(migrated):
    """The owner's email already exists from the old dev login, with no password."""
    import auth
    conn = _raw()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("INSERT INTO users (id, email, first_name) VALUES ('legacy-1', 'Owner@Example.com', 'Owner')")
    cur.execute("INSERT INTO patients (user_id, name) VALUES ('legacy-1', 'Parent One')")
    cur.close()
    conn.close()
    assert auth.is_first_user()
    with pytest.raises(auth.AuthError):
        auth.login("owner@example.com", "correct horse battery")
    user = auth.signup("owner@example.com", "correct horse battery", "Owner")
    assert user["id"] == "legacy-1" and user["role"] == "admin"
    assert auth.login("owner@example.com", "correct horse battery")["id"] == "legacy-1"
    with pytest.raises(auth.AuthError):
        auth.signup("owner@example.com", "another password here", "Owner again")
    assert _query("SELECT count(*) FROM patients WHERE user_id = 'legacy-1'")[0][0] == 1


def test_forgot_password_with_family_reset_code(accounts, monkeypatch):
    import auth
    monkeypatch.delenv("PASSWORD_RESET_SECRET", raising=False)
    with pytest.raises(auth.AuthError):
        auth.reset_password_with_secret("viewer@example.com", "anything", "a brand new password")
    monkeypatch.setenv("PASSWORD_RESET_SECRET", "family-code-123")
    with pytest.raises(auth.AuthError):
        auth.reset_password_with_secret("viewer@example.com", "wrong", "a brand new password")
    with pytest.raises(auth.AuthError):
        auth.reset_password_with_secret("nobody@example.com", "family-code-123", "a brand new password")
    user = auth.reset_password_with_secret("Viewer@Example.com", " family-code-123 ", "a brand new password")
    assert user["id"] == accounts["viewer"]["id"]
    auth.login("viewer@example.com", "a brand new password")
    with pytest.raises(auth.AuthError):
        auth.login("viewer@example.com", "correct horse battery")


def test_invite_bound_to_email_rejects_other_addresses(accounts):
    import auth
    inv = auth.create_invite(accounts["admin"], "editor", email="Sister@Example.com", note="Sister")
    assert inv["email"] == "sister@example.com" and inv["note"] == "Sister"
    with pytest.raises(auth.AuthError):
        auth.signup("someone-else@example.com", "correct horse battery", "X", inv["code"])
    user = auth.signup("sister@example.com", "correct horse battery", "Sister", inv["code"])
    assert user["role"] == "editor"
    with pytest.raises(auth.AuthError):
        auth.create_invite(accounts["admin"], "viewer", email="not-an-email")
    assert auth.invite_link("https://app.example.com/", "abc") == "https://app.example.com/?invite=abc"
    assert "abc" in auth.invite_message("https://app.example.com", {"code": "abc", "email": "sister@example.com"})


def test_login_rejects_wrong_password_and_disabled_accounts(accounts):
    import auth
    assert auth.login("Admin@Example.com", "correct horse battery")["id"] == accounts["admin"]["id"]
    with pytest.raises(auth.AuthError):
        auth.login("admin@example.com", "wrong password here")
    with pytest.raises(auth.AuthError):
        auth.login("nobody@example.com", "correct horse battery")
    import database
    database.set_user_active(accounts["admin"], accounts["viewer"]["id"], False)
    with pytest.raises(auth.AuthError):
        auth.login("viewer@example.com", "correct horse battery")


def test_signup_validation(migrated):
    import auth
    with pytest.raises(auth.AuthError):
        auth.signup("not-an-email", "correct horse battery", "A")
    with pytest.raises(auth.AuthError):
        auth.signup("a@example.com", "short", "A")
    with pytest.raises(auth.AuthError):
        auth.signup("a@example.com", "correct horse battery", "  ")


def test_password_change_and_admin_reset(accounts):
    import auth
    auth.change_password(accounts["editor"], "correct horse battery", "a brand new password")
    with pytest.raises(auth.AuthError):
        auth.login("editor@example.com", "correct horse battery")
    auth.login("editor@example.com", "a brand new password")
    temp = auth.reset_password(accounts["admin"], accounts["editor"]["id"])
    auth.login("editor@example.com", temp)
    with pytest.raises(PermissionError):
        auth.reset_password(accounts["editor"], accounts["admin"]["id"])


# Roles and audit

def test_roles_gate_writes(accounts):
    db = __import__("database")
    pid = db.create_patient(accounts["editor"], "Parent One", age=80)
    with pytest.raises(PermissionError):
        db.create_patient(accounts["viewer"], "Parent Two")
    with pytest.raises(PermissionError):
        db.update_patient(pid, accounts["viewer"], age=81)
    with pytest.raises(PermissionError):
        db.save_vital_reading(pid, accounts["viewer"], "glucose", 100, "mg/dL")
    with pytest.raises(PermissionError):
        db.delete_patient(pid, accounts["editor"])  # admin only
    with pytest.raises(PermissionError):
        db.update_doctor_agent(accounts["editor"], "neurologist", is_active=False)
    # Viewers can read everything.
    assert db.get_patient(pid, accounts["viewer"])["name"] == "Parent One"
    assert len(db.list_patients(accounts["viewer"])) == 1
    db.update_doctor_agent(accounts["admin"], "neurologist", is_active=False)
    assert "neurologist" not in {a["agent_type"] for a in db.get_doctor_agents()}


def test_every_write_is_audited(accounts):
    db = __import__("database")
    pid = db.create_patient(accounts["editor"], "Parent One")
    db.update_patient(pid, accounts["editor"], age=79)
    db.save_vital_reading(pid, accounts["editor"], "glucose", 100, "mg/dL")
    fid = db.save_document(pid, accounts["editor"], "lab.png", "image/png", b"\x89PNG", "lab_report",
                           pages=[{"page_number": 1, "image_png": b"\x89PNG", "width": 1, "height": 1}])
    actions = [r["action"] for r in db.get_audit_log(pid)]
    assert {"patient.create", "patient.update", "vital.create", "document.upload"} <= set(actions)
    assert all(r["user_id"] == accounts["editor"]["id"] for r in db.get_audit_log(pid) if r["action"] != "user.signup")
    assert db.get_document(fid, pid, accounts["viewer"])["sha256"]


def test_no_cross_patient_leakage(accounts):
    """Everything written for patient A must be invisible through patient B's id."""
    db = __import__("database")
    ed = accounts["editor"]
    a = db.create_patient(ed, "Parent A")
    b = db.create_patient(ed, "Parent B")
    db.save_vital_reading(a, ed, "glucose", 111, "mg/dL")
    fa = db.save_document(a, ed, "a.png", "image/png", b"A", "lab_report",
                          pages=[{"page_number": 1, "image_png": b"A", "width": 1, "height": 1}])
    db.add_extracted_items(a, fa, ed, [{"page_number": 1, "item_type": "lab_value", "payload": {"test": "x"}, "confidence": 0.9}])
    db.save_doctor_memory(a, ed, "neurologist", "finding", "only for A")
    conv = db.create_conversation(ed, a, "A chat")
    db.save_chat_message(a, conv, ed, "user", "hello A")

    assert db.get_vitals_history(b, ed) == []
    assert db.get_latest_vitals(b, ed) == []
    assert db.list_documents(b, ed) == []
    assert db.get_document(fa, b, ed) is None
    assert db.get_document_pages(fa, b, ed) == []
    assert db.list_extracted_items(b, ed) == []
    assert db.get_doctor_memories(b, ed) == []
    assert db.get_user_conversations(ed, b) == []
    assert db.get_audit_log(b) == [r for r in db.get_audit_log(b) if r["patient_id"] == b]
    with pytest.raises(PermissionError):
        db.save_chat_message(b, conv, ed, "user", "wrong patient")
    # And A's data is all there.
    assert len(db.get_vitals_history(a, ed)) == 1
    assert len(db.list_documents(a, ed)) == 1
    assert len(db.list_extracted_items(a, ed, status="pending")) == 1


def test_extracted_items_review_flow(accounts):
    db = __import__("database")
    ed = accounts["editor"]
    pid = db.create_patient(ed, "Parent One")
    fid = db.save_document(pid, ed, "rx.pdf", "application/pdf", b"%PDF", "prescription",
                           pages=[{"page_number": 1, "image_png": b"P", "width": 1, "height": 1},
                                  {"page_number": 2, "image_png": b"P", "width": 1, "height": 1}])
    ids = db.add_extracted_items(pid, fid, ed, [
        {"page_number": 1, "item_type": "medication", "payload": {"name": "Metformin"}, "confidence": 0.95},
        {"page_number": 2, "item_type": "medication", "payload": {"name": "??"}, "confidence": 0.3},
    ])
    db.set_extracted_item_status(ids[0], pid, ed, "confirmed", payload={"name": "Metformin", "dose": "500 mg"})
    db.set_extracted_item_status(ids[1], pid, ed, "rejected")
    items = {i["id"]: i for i in db.list_extracted_items(pid, ed, file_id=fid)}
    assert items[ids[0]]["status"] == "confirmed" and items[ids[0]]["payload"]["dose"] == "500 mg"
    assert items[ids[1]]["status"] == "rejected"
    assert items[ids[0]]["reviewed_by"] == ed["id"]
    assert db.list_extracted_items(pid, ed, status="pending") == []
    pages = db.get_document_pages(fid, pid, ed, with_images=False)
    assert [p["page_number"] for p in pages] == [1, 2]


def test_pool_recovers_when_the_server_drops_the_connection(migrated):
    """Hosted Postgres closes idle sessions; the next query must not fail."""
    with migrated.db_cursor() as cur:
        cur.execute("SELECT pg_backend_pid() AS pid")
        pid = cur.fetchone()["pid"]
    # Kill that backend from a separate raw connection, as the server would on idle timeout.
    conn = _raw()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
    cur.close()
    conn.close()
    # The pool still holds the dead connection; the next call must replace it transparently.
    assert len(migrated.get_doctor_agents(active_only=False)) == 15
    with migrated.db_cursor() as cur:
        cur.execute("SELECT pg_backend_pid() AS pid")
        assert cur.fetchone()["pid"] != pid


def test_pool_serves_more_threads_than_connections(migrated):
    errors = []

    def work():
        try:
            for _ in range(10):
                migrated.get_doctor_agents()
        except Exception as e:
            errors.append(repr(e))

    threads = [threading.Thread(target=work) for _ in range(migrated.POOL_MAX * 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_remembered_sign_in_sessions(accounts):
    import auth
    admin = accounts["admin"]
    token = auth.create_session(admin)
    assert auth.user_from_session(token)["id"] == admin["id"]
    assert auth.user_from_session("not a token") is None
    assert auth.user_from_session(None) is None
    # Changing the password ends every remembered session.
    auth.change_password(admin, "correct horse battery", "a brand new passphrase")
    assert auth.user_from_session(token) is None
    token2 = auth.create_session(auth.login("admin@example.com", "a brand new passphrase"))
    assert auth.user_from_session(token2) is not None
    auth.end_session(token2)
    assert auth.user_from_session(token2) is None
    # An expired session is ignored.
    token3 = auth.create_session(admin, days=0)
    with _raw() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE sessions SET expires_at = expires_at - interval '1 day'")
        conn.commit()
    assert auth.user_from_session(token3) is None
    # A disabled account cannot be resumed.
    token4 = auth.create_session(accounts["viewer"])
    import database
    database.set_user_active(admin, accounts["viewer"]["id"], False)
    assert auth.user_from_session(token4) is None


def test_last_patient_is_remembered_per_user(accounts, migrated):
    import auth
    admin = accounts["admin"]
    pid = migrated.create_patient(admin, "Parent One")
    migrated.set_last_patient(admin, pid)
    assert auth.login("admin@example.com", "correct horse battery")["last_patient_id"] == pid
    assert auth.user_from_session(auth.create_session(admin))["last_patient_id"] == pid


def test_upgrade_tolerates_a_schema_already_synced_by_the_host(db):
    """Replit applies its own schema diff to production before the app starts,
    so the tables and columns of 0004 to 0007 can already exist while
    alembic_version still says 0003. Upgrading must then succeed."""
    db.run_migrations()
    conn = _raw()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("UPDATE alembic_version SET version_num = '0003'")
    cur.close()
    conn.close()
    assert db.migration_plan()["current_revision"] == "0003"
    plan = db.run_migrations()
    assert db.migration_plan()["current_revision"] == HEAD
    assert _tables() == EXPECTED_TABLES
    cols = {r[0] for r in _query("SELECT column_name FROM information_schema.columns WHERE table_name = 'doctor_memory'")}
    assert {"source_run_id", "source_consultation_id", "created_by", "updated_at", "updated_by"} <= cols
