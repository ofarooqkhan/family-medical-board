"""Medication list with change history, duplicate detection, and
reconciliation from confirmed extracted items.

Follows the database.py conventions: every function takes patient_id and
user, every read is scoped by patient_id, every write requires the editor
role and adds an audit_log row. Each add, update, or stop also writes a
medication_changes row so the family can see what changed and when.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from database import _assert_patient, audit, db_cursor, require_role
from utils import json_dumps_safe

EDITABLE_FIELDS = ("name", "generic_name", "dose", "route", "frequency", "indication", "prescriber",
                   "start_date", "end_date", "status", "notes", "source_file_id", "source_page")
DATE_FIELDS = ("start_date", "end_date")
RECONCILE_FIELDS = ("dose", "route", "frequency", "prescriber", "start_date", "generic_name")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_date(value: Any) -> _dt.date | None:
    """Accept a date, a datetime, or an ISO string. Unparseable values become None."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _dt.date.fromisoformat(text)
        except ValueError:
            pass
        try:
            return _dt.datetime.fromisoformat(text).date()
        except ValueError:
            return None
    return None


def normalize_name(name: Any) -> str:
    """Lowercase, strip, and drop everything that is not a letter or digit."""
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(name).lower().strip())


def _snapshot(row: dict) -> dict:
    """The editable fields of a medication row, dates as ISO strings, for JSONB."""
    out = {}
    for k in EDITABLE_FIELDS:
        v = row.get(k)
        if isinstance(v, (_dt.date, _dt.datetime)):
            v = v.isoformat()
        out[k] = v
    return out


def _clean_fields(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k not in EDITABLE_FIELDS:
            continue
        if k in DATE_FIELDS:
            v = parse_date(v)
        out[k] = v
    return out


def _get_for_update(cur, medication_id: int, patient_id: int) -> dict:
    cur.execute("SELECT * FROM medications WHERE id = %s AND patient_id = %s FOR UPDATE", (medication_id, patient_id))
    row = cur.fetchone()
    if row is None:
        raise PermissionError(f"no medication {medication_id} for patient {patient_id}")
    return row


def _record_change(cur, user: dict, patient_id: int, medication_id: int, change_type: str,
                   before: dict | None, after: dict | None, source_file_id: int | None) -> int:
    cur.execute("""
        INSERT INTO medication_changes (medication_id, patient_id, change_type, before, after, changed_by, source_file_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
    """, (medication_id, patient_id, change_type, json_dumps_safe(before), json_dumps_safe(after), user["id"], source_file_id))
    return cur.fetchone()["id"]


def _apply_update(cur, user: dict, medication_id: int, patient_id: int, fields: dict,
                  change_type: str | None = None) -> tuple[dict, dict]:
    """Update editable fields inside an open cursor and record the change. Returns (before, after)."""
    before_row = _get_for_update(cur, medication_id, patient_id)
    before = _snapshot(before_row)
    sets = [f"{k} = %s" for k in fields]
    vals: list[Any] = list(fields.values())
    vals.extend([medication_id, patient_id])
    cur.execute(f"UPDATE medications SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND patient_id = %s", vals)
    cur.execute("SELECT * FROM medications WHERE id = %s AND patient_id = %s", (medication_id, patient_id))
    after = _snapshot(cur.fetchone())
    if change_type is None:
        change_type = "stopped" if after.get("status") == "stopped" and before.get("status") != "stopped" else "changed"
    _record_change(cur, user, patient_id, medication_id, change_type, before, after, fields.get("source_file_id"))
    return before, after


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def list_medications(patient_id: int, user: dict, status: str | None = None):
    require_role(user, "viewer")
    with db_cursor() as cur:
        if status:
            cur.execute("SELECT * FROM medications WHERE patient_id = %s AND status = %s ORDER BY status, name, id",
                        (patient_id, status))
        else:
            cur.execute("SELECT * FROM medications WHERE patient_id = %s ORDER BY status, name, id", (patient_id,))
        return cur.fetchall()


def get_medication(medication_id: int, patient_id: int, user: dict):
    require_role(user, "viewer")
    with db_cursor() as cur:
        cur.execute("SELECT * FROM medications WHERE id = %s AND patient_id = %s", (medication_id, patient_id))
        return cur.fetchone()


def medication_history(patient_id: int, user: dict, medication_id: int | None = None):
    """medication_changes rows, newest first, with the medication name and who changed it."""
    require_role(user, "viewer")
    sql = """
        SELECT mc.*, m.name AS medication_name, u.display_name AS changed_by_name
        FROM medication_changes mc
        JOIN medications m ON m.id = mc.medication_id
        LEFT JOIN users u ON u.id = mc.changed_by
        WHERE mc.patient_id = %s
    """
    params: list[Any] = [patient_id]
    if medication_id is not None:
        sql += " AND mc.medication_id = %s"
        params.append(medication_id)
    sql += " ORDER BY mc.changed_at DESC, mc.id DESC"
    with db_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def add_medication(patient_id: int, user: dict, name: str, dose: str | None = None, route: str | None = None,
                   frequency: str | None = None, indication: str | None = None, prescriber: str | None = None,
                   start_date=None, end_date=None, notes: str | None = None, source_file_id: int | None = None,
                   source_page: int | None = None, extracted_item_id: int | None = None,
                   generic_name: str | None = None) -> int:
    require_role(user, "editor")
    name = (name or "").strip()
    if not name:
        raise ValueError("medication name is required")
    with db_cursor(commit=True) as cur:
        _assert_patient(cur, patient_id)
        cur.execute("""
            INSERT INTO medications (patient_id, name, generic_name, dose, route, frequency, indication, prescriber,
                                     start_date, end_date, notes, source_file_id, source_page, extracted_item_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """, (patient_id, name, generic_name, dose, route, frequency, indication, prescriber, parse_date(start_date),
              parse_date(end_date), notes, source_file_id, source_page, extracted_item_id, user["id"]))
        row = cur.fetchone()
        med_id = row["id"]
        _record_change(cur, user, patient_id, med_id, "added", None, _snapshot(row), source_file_id)
        audit(cur, user, patient_id, "medication.add", "medication", med_id, {"name": name, "dose": dose, "prescriber": prescriber})
        return med_id


def update_medication(medication_id: int, patient_id: int, user: dict, **fields) -> None:
    require_role(user, "editor")
    clean = _clean_fields(fields)
    if not clean:
        return
    with db_cursor(commit=True) as cur:
        before, after = _apply_update(cur, user, medication_id, patient_id, clean)
        changed = {k: after.get(k) for k in clean if before.get(k) != after.get(k)}
        audit(cur, user, patient_id, "medication.update", "medication", medication_id,
              {"fields": sorted(clean), "changed": changed})


def stop_medication(medication_id: int, patient_id: int, user: dict, end_date=None, reason: str | None = None) -> None:
    require_role(user, "editor")
    end = parse_date(end_date) or _dt.date.today()
    with db_cursor(commit=True) as cur:
        current = _get_for_update(cur, medication_id, patient_id)
        fields: dict[str, Any] = {"status": "stopped", "end_date": end}
        reason = (reason or "").strip()
        if reason:
            existing = (current.get("notes") or "").strip()
            line = f"Stopped {end.isoformat()}: {reason}"
            fields["notes"] = f"{existing}\n{line}" if existing else line
        _apply_update(cur, user, medication_id, patient_id, fields, change_type="stopped")
        audit(cur, user, patient_id, "medication.stop", "medication", medication_id,
              {"end_date": end.isoformat(), "reason": reason or None})


# ---------------------------------------------------------------------------
# Duplicates and reconciliation (pure Python over the active list)
# ---------------------------------------------------------------------------

def _same_drug(a: dict, b: dict) -> bool:
    na, nb = normalize_name(a.get("name")), normalize_name(b.get("name"))
    ga, gb = normalize_name(a.get("generic_name")), normalize_name(b.get("generic_name"))
    if na and na == nb:
        return True
    if ga and ga in (nb, gb):
        return True
    if gb and gb in (na, ga):
        return True
    return False


def _group_same_drug(meds: list[dict]) -> list[list[dict]]:
    """Union-find over the list: two rows join a group when _same_drug says so."""
    parent = list(range(len(meds)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(meds)):
        for j in range(i + 1, len(meds)):
            if _same_drug(meds[i], meds[j]):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    groups: dict[int, list[dict]] = {}
    for i, m in enumerate(meds):
        groups.setdefault(find(i), []).append(m)
    return [g for _, g in sorted(groups.items())]


def find_duplicates(patient_id: int, user: dict) -> dict:
    """Groups of active medications that look like the same drug, and groups
    with more than one distinct prescriber.

    Returns {"duplicates": [[row, ...], ...], "multi_prescriber": [{"name", "prescribers", "medications"}, ...]}.
    """
    require_role(user, "viewer")
    meds = [dict(m) for m in list_medications(patient_id, user, status="active")]
    meds.sort(key=lambda m: m["id"])
    duplicates: list[list[dict]] = []
    multi: list[dict] = []
    for group in _group_same_drug(meds):
        if len(group) > 1:
            duplicates.append(group)
        prescribers = sorted({(m.get("prescriber") or "").strip() for m in group if (m.get("prescriber") or "").strip()})
        if len(prescribers) > 1:
            multi.append({"name": normalize_name(group[0].get("name")), "prescribers": prescribers, "medications": group})
    return {"duplicates": duplicates, "multi_prescriber": multi}


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _differs(old: Any, new: Any) -> bool:
    if isinstance(old, str) and isinstance(new, str):
        return old.strip() != new.strip()
    return old != new


def reconcile_from_items(patient_id: int, user: dict, items: list[dict]) -> dict:
    """Apply confirmed extracted items of item_type "medication" to the active list.

    A payload whose normalized name matches no active medication is added. One
    that matches is updated only for fields that differ and are non-empty in
    the payload. Every change goes through add_medication or update_medication,
    so history and audit rows are kept.
    """
    require_role(user, "editor")
    active = [dict(m) for m in list_medications(patient_id, user, status="active")]
    by_norm: dict[str, dict] = {}
    for m in active:
        by_norm.setdefault(normalize_name(m["name"]), m)
    result: dict[str, list[int]] = {"added": [], "updated": [], "unchanged": []}
    for item in items or []:
        if item.get("item_type") not in (None, "medication"):
            continue
        payload = item.get("payload") or {}
        name = (payload.get("name") or "").strip()
        key = normalize_name(name)
        if not key:
            continue
        existing = by_norm.get(key)
        if existing is None:
            med_id = add_medication(
                patient_id, user, name,
                dose=payload.get("dose"), route=payload.get("route"), frequency=payload.get("frequency"),
                indication=payload.get("indication"), prescriber=payload.get("prescriber"),
                start_date=parse_date(payload.get("start_date")), generic_name=payload.get("generic_name"),
                source_file_id=item.get("file_id"), source_page=item.get("page_number"), extracted_item_id=item.get("id"),
            )
            result["added"].append(med_id)
            by_norm[key] = {"id": med_id, "name": name, "status": "active",
                            **{f: (parse_date(payload.get(f)) if f == "start_date" else payload.get(f)) for f in RECONCILE_FIELDS}}
            continue
        changes: dict[str, Any] = {}
        for f in RECONCILE_FIELDS:
            new = payload.get(f)
            if f == "start_date":
                new = parse_date(new)
            if _nonempty(new) and _differs(existing.get(f), new):
                changes[f] = new
        if not changes:
            result["unchanged"].append(existing["id"])
            continue
        if item.get("file_id") is not None:
            changes["source_file_id"] = item.get("file_id")
            changes["source_page"] = item.get("page_number")
        update_medication(existing["id"], patient_id, user, **changes)
        existing.update(changes)
        result["updated"].append(existing["id"])
    return result
