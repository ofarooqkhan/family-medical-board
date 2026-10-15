"""Shared fixtures for the database-backed tests.

They run only when TEST_DATABASE_URL points at a throwaway Postgres. Every
test starts from an empty public schema. Nothing here touches the
DATABASE_URL the app uses. Tests that need these fixtures must carry a
skipif mark on TEST_DATABASE_URL themselves (see tests/test_database.py).
"""
import os

import psycopg2
import pytest

TEST_URL = os.environ.get("TEST_DATABASE_URL")


def _reset_schema():
    conn = psycopg2.connect(TEST_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    cur.close()
    conn.close()


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    # Most tests exercise the manual confirm step; auto-add has its own tests.
    monkeypatch.setenv("AUTO_ADD_ITEMS", "0")
    import database
    database.close_pool()
    _reset_schema()
    yield database
    database.close_pool()


@pytest.fixture
def migrated(db):
    db.run_migrations()
    return db


@pytest.fixture
def accounts(migrated):
    """An admin, an editor, and a viewer created through the real signup flow."""
    import auth
    admin = auth.signup("admin@example.com", "correct horse battery", "Admin")
    inv_e = auth.create_invite(admin, "editor")
    editor = auth.signup("editor@example.com", "correct horse battery", "Editor", inv_e["code"])
    inv_v = auth.create_invite(admin, "viewer")
    viewer = auth.signup("viewer@example.com", "correct horse battery", "Viewer", inv_v["code"])
    return {"admin": admin, "editor": editor, "viewer": viewer}
