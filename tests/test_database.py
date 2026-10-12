"""Database tests. Run only when TEST_DATABASE_URL points at a throwaway Postgres.

Every test starts from an empty public schema. Nothing here touches the
DATABASE_URL the app uses.
"""
import os

import psycopg2
import pytest

TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")

# The eight tables as the pre-Alembic Replit app created them, without the
# two columns revision 0002 adds. Used to simulate the live database.
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
"""

EXPECTED_TABLES = {
    "users", "patients", "conversations", "patient_files", "vitals_history",
    "doctor_agents", "doctor_memory", "chat_history", "alembic_version",
}


def _raw():
    return psycopg2.connect(TEST_URL)


def _reset_schema():
    conn = _raw()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    cur.close()
    conn.close()


def _columns(table):
    conn = _raw()
    cur = conn.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s "
        "ORDER BY ordinal_position", (table,))
    cols = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return cols


def _tables():
    conn = _raw()
    cur = conn.cursor()
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    names = {r[0] for r in cur.fetchall()}
    cur.close()
    conn.close()
    return names


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    import database
    database.close_pool()
    _reset_schema()
    yield database
    database.close_pool()


def test_fresh_database_reaches_head(db):
    plan = db.run_migrations()
    assert plan["will_stamp"] is None
    assert _tables() == EXPECTED_TABLES
    assert _columns("vitals_history")[-2:] == ["recorded_at", "source_file_id"]
    assert db.migration_plan()["current_revision"] == "0002"
    assert len(db.get_doctor_agents(active_only=False)) == 6


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
    assert "recorded_at" in _columns("vitals_history")
    assert "source_file_id" in _columns("vitals_history")
    assert db.migration_plan()["current_revision"] == "0002"

    with db.db_cursor() as c:
        c.execute("SELECT (SELECT count(*) FROM users) u, (SELECT count(*) FROM patients) p, "
                  "(SELECT count(*) FROM vitals_history) v")
        row = c.fetchone()
    assert (row["u"], row["p"], row["v"]) == (1, 2, 2)


def test_second_run_is_a_no_op(db):
    db.run_migrations()
    pid = db.create_patient_for_user(db.get_or_create_user("u1")["id"], "Parent One")
    db.save_vital_reading(pid, "u1", "glucose", 100, "mg/dL")
    before = db.migration_plan()
    plan = db.run_migrations()
    assert plan["will_stamp"] is None
    assert db.migration_plan() == before
    assert len(db.get_vitals_history(pid, "u1")) == 1


def test_owned_patient_is_hidden_from_other_user(db):
    db.run_migrations()
    db.get_or_create_user("u1")
    db.get_or_create_user("u2")
    pid = db.create_patient_for_user("u1", "Parent One")
    assert db.get_patient(pid, "u1") is not None
    assert db.get_patient(pid, "u2") is None
    with pytest.raises(PermissionError):
        db.update_patient(pid, "u2", name="x")
    with pytest.raises(PermissionError):
        db.save_vital_reading(pid, "u2", "glucose", 1, "mg/dL")
    cid = db.create_conversation("u1", pid, "t")
    with pytest.raises(PermissionError):
        db.save_chat_message_to_conversation(pid, cid, "u2", "user", "hi")
    assert db.get_conversation_history(cid, "u2") == []


def test_pool_serves_more_threads_than_connections(db):
    import threading
    db.run_migrations()
    errors = []

    def work():
        try:
            for _ in range(10):
                db.get_doctor_agents()
        except Exception as e:
            errors.append(repr(e))

    threads = [threading.Thread(target=work) for _ in range(db.POOL_MAX * 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
