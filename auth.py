"""Built-in accounts: email and password, invite codes, roles.

No third-party provider. The first account created on an empty database is
the admin. Every later account needs an invite code made by an admin, which
fixes the new account's role. Passwords are hashed with bcrypt.

Portable: the rest of the app only sees the user dict this module returns
(id, email, display_name, role, language, is_active). A hosted provider can
replace this module behind the same two functions, login() and signup().
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt

from database import ROLE_RANK, audit, db_cursor, require_role

MIN_PASSWORD_LENGTH = 10
INVITE_DAYS = 14
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

PUBLIC_USER_COLUMNS = "id, email, display_name, role, language, is_active, created_at, last_login_at"


def _utcnow() -> datetime:
    # Columns are TIMESTAMP without time zone and hold UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AuthError(Exception):
    """Shown to the person as is. Never includes which part of a login failed."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


def _validate_new_account(email: str, password: str, display_name: str) -> None:
    if not _EMAIL_RE.match(email or ""):
        raise AuthError("Enter a valid email address.")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if not (display_name or "").strip():
        raise AuthError("Enter your name.")


def is_first_user() -> bool:
    with db_cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM users WHERE password_hash IS NOT NULL")
        return cur.fetchone()["n"] == 0


def signup(email: str, password: str, display_name: str, invite_code: str | None = None) -> dict:
    """Create an account. First account becomes admin; later ones need a valid invite."""
    email = (email or "").strip().lower()
    _validate_new_account(email, password, display_name)
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT count(*) AS n FROM users WHERE password_hash IS NOT NULL")
        first = cur.fetchone()["n"] == 0
        cur.execute("SELECT id, password_hash FROM users WHERE lower(email) = %s", (email,))
        existing = cur.fetchone()
        if existing and existing["password_hash"]:
            raise AuthError("An account with that email already exists. Use Sign in, or Forgot password.")
        # A row with this email but no password is a leftover from the old
        # development login. Signing up claims it, keeping its id and history.
        legacy_id = existing["id"] if existing else None
        role = "admin"
        invite = None
        if not first:
            code = (invite_code or "").strip()
            if not code:
                raise AuthError("An invite code is required.")
            cur.execute("SELECT * FROM invites WHERE code = %s", (code,))
            invite = cur.fetchone()
            if invite is None or invite["used_at"] is not None:
                raise AuthError("That invite code is not valid.")
            if invite["expires_at"] and invite["expires_at"] < _utcnow():
                raise AuthError("That invite code has expired.")
            role = invite["role"]
        if legacy_id:
            user_id = legacy_id
            cur.execute(f"""
                UPDATE users SET first_name = %s, display_name = %s, password_hash = %s, role = %s, is_active = TRUE,
                       last_login_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s RETURNING {PUBLIC_USER_COLUMNS}
            """, (display_name.strip(), display_name.strip(), hash_password(password), role, user_id))
        else:
            user_id = str(uuid.uuid4())
            cur.execute(f"""
                INSERT INTO users (id, email, first_name, display_name, password_hash, role, is_active, last_login_at)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE, CURRENT_TIMESTAMP) RETURNING {PUBLIC_USER_COLUMNS}
            """, (user_id, email, display_name.strip(), display_name.strip(), hash_password(password), role))
        user = dict(cur.fetchone())
        if invite is not None:
            cur.execute("UPDATE invites SET used_by = %s, used_at = CURRENT_TIMESTAMP WHERE id = %s", (user_id, invite["id"]))
        audit(cur, user, None, "user.signup", "user", None,
              {"role": role, "invite_id": invite["id"] if invite else None, "claimed_legacy": bool(legacy_id)})
        return user


def login(email: str, password: str) -> dict:
    email = (email or "").strip().lower()
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT * FROM users WHERE lower(email) = %s", (email,))
        row = cur.fetchone()
        if row is None or not verify_password(password or "", row.get("password_hash")):
            # Same message either way so emails cannot be probed.
            raise AuthError("Email or password is incorrect.")
        if not row["is_active"]:
            raise AuthError("This account has been disabled. Ask an admin.")
        cur.execute(f"UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING {PUBLIC_USER_COLUMNS}", (row["id"],))
        user = dict(cur.fetchone())
        audit(cur, user, None, "user.login")
        return user


def change_password(user: dict, current_password: str, new_password: str) -> None:
    require_role(user, "viewer")
    if len(new_password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT password_hash FROM users WHERE id = %s", (user["id"],))
        row = cur.fetchone()
        if row is None or not verify_password(current_password or "", row["password_hash"]):
            raise AuthError("Current password is incorrect.")
        cur.execute("UPDATE users SET password_hash = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (hash_password(new_password), user["id"]))
        audit(cur, user, None, "user.password")


def reset_secret_configured() -> bool:
    return bool(os.environ.get("PASSWORD_RESET_SECRET"))


def reset_password_with_secret(email: str, secret: str, new_password: str) -> dict:
    """Forgot password without email delivery: the family keeps a reset code in
    the PASSWORD_RESET_SECRET environment variable (Replit Secrets). Anyone
    holding it can set a new password for an existing account."""
    configured = os.environ.get("PASSWORD_RESET_SECRET") or ""
    if not configured:
        raise AuthError("Password reset is not set up. Ask the family admin to reset your password from Settings.")
    if not hmac.compare_digest(configured.strip(), (secret or "").strip()):
        raise AuthError("Reset code is incorrect.")
    if len(new_password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    email = (email or "").strip().lower()
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT * FROM users WHERE lower(email) = %s", (email,))
        row = cur.fetchone()
        if row is None:
            raise AuthError("No account with that email.")
        cur.execute(f"""UPDATE users SET password_hash = %s, is_active = TRUE, updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s RETURNING {PUBLIC_USER_COLUMNS}""", (hash_password(new_password), row["id"]))
        user = dict(cur.fetchone())
        audit(cur, user, None, "user.password_reset_self")
        return user


def reset_password(admin: dict, target_user_id: str) -> str:
    """Admin sets a temporary password for someone who forgot theirs. Returned once."""
    require_role(admin, "admin")
    temp = secrets.token_urlsafe(9)
    with db_cursor(commit=True) as cur:
        cur.execute("UPDATE users SET password_hash = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (hash_password(temp), target_user_id))
        if cur.rowcount != 1:
            raise AuthError("No such user.")
        audit(cur, admin, None, "user.password_reset", "user", None, {"user_id": target_user_id})
    return temp


def create_invite(admin: dict, role: str = "viewer", days: int = INVITE_DAYS) -> dict:
    require_role(admin, "admin")
    if role not in ROLE_RANK:
        raise ValueError(f"unknown role {role}")
    code = secrets.token_urlsafe(12)
    with db_cursor(commit=True) as cur:
        cur.execute("""INSERT INTO invites (code, role, created_by, expires_at) VALUES (%s, %s, %s, %s) RETURNING *""",
                    (code, role, admin["id"], _utcnow() + timedelta(days=days)))
        row = dict(cur.fetchone())
        audit(cur, admin, None, "invite.create", "invite", row["id"], {"role": role})
        return row


def list_invites(admin: dict):
    require_role(admin, "admin")
    with db_cursor() as cur:
        cur.execute("""SELECT i.*, u.display_name AS used_by_name FROM invites i LEFT JOIN users u ON u.id = i.used_by
                       ORDER BY i.created_at DESC""")
        return cur.fetchall()


def revoke_invite(admin: dict, invite_id: int) -> None:
    require_role(admin, "admin")
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM invites WHERE id = %s AND used_at IS NULL", (invite_id,))
        audit(cur, admin, None, "invite.revoke", "invite", invite_id)
