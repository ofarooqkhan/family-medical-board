"""Appointments: a per-patient calendar with reminders and iCalendar export.

Follows the database.py conventions: every function takes patient_id and
user (except due_reminders, which spans all patients), reads are scoped by
patient_id, writes require the editor role and add an audit_log row.

Timestamps are stored as naive TIMESTAMP columns and are treated as UTC
everywhere, including the ICS export.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable
from urllib.parse import quote, urlencode

from database import _assert_patient, audit, db_cursor, require_role

EDITABLE_FIELDS = ("title", "kind", "doctor_name", "specialty", "location", "starts_at", "ends_at",
                   "reminder_minutes", "status", "notes")
TIMESTAMP_FIELDS = ("starts_at", "ends_at")
DEFAULT_REMINDER_MINUTES = 1440
DEFAULT_DURATION = _dt.timedelta(minutes=30)
PRODID = "-//Family Medical Board//Appointments//EN"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def to_naive_utc(value: Any) -> _dt.datetime | None:
    """Accept a datetime, a date, or an ISO string; return a naive UTC datetime or None."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = _dt.datetime.fromisoformat(text)
        except ValueError:
            try:
                value = _dt.datetime.combine(_dt.date.fromisoformat(text), _dt.time())
            except ValueError:
                return None
    if isinstance(value, _dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time())
    return None


def _clean_fields(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k not in EDITABLE_FIELDS:
            continue
        if k in TIMESTAMP_FIELDS:
            v = to_naive_utc(v)
        out[k] = v
    return out


def _get_for_update(cur, appointment_id: int, patient_id: int) -> dict:
    cur.execute("SELECT * FROM appointments WHERE id = %s AND patient_id = %s FOR UPDATE", (appointment_id, patient_id))
    row = cur.fetchone()
    if row is None:
        raise PermissionError(f"no appointment {appointment_id} for patient {patient_id}")
    return row


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def list_appointments(patient_id: int, user: dict, upcoming_only: bool = False, limit: int = 200):
    require_role(user, "viewer")
    with db_cursor() as cur:
        if upcoming_only:
            cur.execute("""SELECT * FROM appointments WHERE patient_id = %s AND status = 'scheduled' AND starts_at >= %s
                           ORDER BY starts_at ASC, id ASC LIMIT %s""", (patient_id, _utcnow(), limit))
        else:
            cur.execute("SELECT * FROM appointments WHERE patient_id = %s ORDER BY starts_at ASC, id ASC LIMIT %s",
                        (patient_id, limit))
        return cur.fetchall()


def get_appointment(appointment_id: int, patient_id: int, user: dict):
    require_role(user, "viewer")
    with db_cursor() as cur:
        cur.execute("SELECT * FROM appointments WHERE id = %s AND patient_id = %s", (appointment_id, patient_id))
        return cur.fetchone()


def due_reminders(user: dict, now=None):
    """Scheduled appointments across all non-archived patients whose reminder
    window is open: starts_at - reminder_minutes <= now < starts_at.
    Rows carry the patient's name as patient_name. Viewer role is enough."""
    require_role(user, "viewer")
    now = to_naive_utc(now) or _utcnow()
    with db_cursor() as cur:
        cur.execute("""
            SELECT a.*, p.name AS patient_name
            FROM appointments a JOIN patients p ON p.id = a.patient_id
            WHERE NOT p.is_archived AND a.status = 'scheduled'
              AND a.starts_at - make_interval(mins => COALESCE(a.reminder_minutes, %s)) <= %s
              AND %s < a.starts_at
            ORDER BY a.starts_at ASC, a.id ASC
        """, (DEFAULT_REMINDER_MINUTES, now, now))
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def add_appointment(patient_id: int, user: dict, title: str, starts_at, kind: str = "visit",
                    doctor_name: str | None = None, specialty: str | None = None, location: str | None = None,
                    ends_at=None, reminder_minutes: int = DEFAULT_REMINDER_MINUTES, notes: str | None = None) -> int:
    require_role(user, "editor")
    title = (title or "").strip()
    if not title:
        raise ValueError("appointment title is required")
    start = to_naive_utc(starts_at)
    if start is None:
        raise ValueError("starts_at is required")
    with db_cursor(commit=True) as cur:
        _assert_patient(cur, patient_id)
        cur.execute("""
            INSERT INTO appointments (patient_id, title, kind, doctor_name, specialty, location, starts_at, ends_at,
                                      reminder_minutes, notes, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (patient_id, title, kind or "visit", doctor_name, specialty, location, start, to_naive_utc(ends_at),
              reminder_minutes, notes, user["id"]))
        appt_id = cur.fetchone()["id"]
        audit(cur, user, patient_id, "appointment.add", "appointment", appt_id,
              {"title": title, "starts_at": start.isoformat(), "doctor_name": doctor_name})
        return appt_id


def update_appointment(appointment_id: int, patient_id: int, user: dict, **fields) -> None:
    require_role(user, "editor")
    clean = _clean_fields(fields)
    if not clean:
        return
    with db_cursor(commit=True) as cur:
        _get_for_update(cur, appointment_id, patient_id)
        sets = [f"{k} = %s" for k in clean]
        vals: list[Any] = list(clean.values())
        vals.extend([appointment_id, patient_id])
        cur.execute(f"UPDATE appointments SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND patient_id = %s", vals)
        audit(cur, user, patient_id, "appointment.update", "appointment", appointment_id,
              {k: (v.isoformat() if isinstance(v, _dt.datetime) else v) for k, v in clean.items()})


def delete_appointment(appointment_id: int, patient_id: int, user: dict) -> None:
    require_role(user, "editor")
    with db_cursor(commit=True) as cur:
        row = _get_for_update(cur, appointment_id, patient_id)
        cur.execute("DELETE FROM appointments WHERE id = %s AND patient_id = %s", (appointment_id, patient_id))
        audit(cur, user, patient_id, "appointment.delete", "appointment", appointment_id, {"title": row["title"]})


# ---------------------------------------------------------------------------
# iCalendar export (pure, no database)
# ---------------------------------------------------------------------------

def ics_escape(text: Any) -> str:
    """Escape a TEXT value per RFC 5545 section 3.3.11."""
    if text is None:
        return ""
    s = str(text)
    s = s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return s


def ics_timestamp(value: Any) -> str:
    """Format as UTC YYYYMMDDTHHMMSSZ, treating naive values as UTC."""
    dt = to_naive_utc(value)
    if dt is None:
        raise ValueError(f"not a timestamp: {value!r}")
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _fold(line: str) -> list[str]:
    """Fold a content line to at most 75 octets per physical line (RFC 5545 section 3.1)."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return [line]
    out: list[str] = []
    limit = 75
    pos = 0
    while pos < len(data):
        end = min(pos + limit, len(data))
        # Do not split inside a multi-byte UTF-8 sequence.
        while end < len(data) and (data[end] & 0xC0) == 0x80:
            end -= 1
        out.append(data[pos:end].decode("utf-8"))
        pos = end
        limit = 74  # continuation lines start with one space
    return [out[0]] + [" " + rest for rest in out[1:]]


def to_ics(appointments: Iterable[dict], calendar_name: str = "Family Medical Board") -> str:
    """Render appointment rows as an iCalendar text with one VEVENT per row."""
    stamp = _utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(calendar_name)}",
    ]
    for i, a in enumerate(appointments):
        start = to_naive_utc(a.get("starts_at"))
        if start is None:
            continue
        end = to_naive_utc(a.get("ends_at")) or (start + DEFAULT_DURATION)
        appt_id = a.get("id")
        uid = f"appointment-{appt_id}@family-medical-board" if appt_id is not None else f"appointment-{stamp}-{i}@family-medical-board"
        reminder = a.get("reminder_minutes")
        if reminder is None:
            reminder = DEFAULT_REMINDER_MINUTES
        reminder = max(0, int(reminder))
        description_parts = []
        if a.get("patient_name"):
            description_parts.append(f"Patient: {a['patient_name']}")
        if a.get("doctor_name"):
            doc = f"Doctor: {a['doctor_name']}"
            if a.get("specialty"):
                doc += f" ({a['specialty']})"
            description_parts.append(doc)
        elif a.get("specialty"):
            description_parts.append(f"Specialty: {a['specialty']}")
        if a.get("kind"):
            description_parts.append(f"Kind: {a['kind']}")
        if a.get("notes"):
            description_parts.append(f"Notes: {a['notes']}")
        status = (a.get("status") or "scheduled").lower()
        ics_status = {"scheduled": "CONFIRMED", "cancelled": "CANCELLED", "canceled": "CANCELLED"}.get(status, "TENTATIVE")
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{ics_timestamp(start)}",
            f"DTEND:{ics_timestamp(end)}",
            f"SUMMARY:{ics_escape(a.get('title') or 'Appointment')}",
            f"LOCATION:{ics_escape(a.get('location'))}",
            f"DESCRIPTION:{ics_escape(chr(10).join(description_parts))}",
            f"STATUS:{ics_status}",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{ics_escape(a.get('title') or 'Appointment')}",
            f"TRIGGER:-PT{reminder}M",
            "END:VALARM",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    physical: list[str] = []
    for line in lines:
        physical.extend(_fold(line))
    return "\r\n".join(physical) + "\r\n"


# ---------------------------------------------------------------------------
# Add-to-calendar links (pure, no database)
# ---------------------------------------------------------------------------

def _event_fields(a: dict) -> tuple[_dt.datetime, _dt.datetime, str, str, str]:
    start = to_naive_utc(a.get("starts_at"))
    if start is None:
        raise ValueError("appointment has no start time")
    end = to_naive_utc(a.get("ends_at")) or (start + DEFAULT_DURATION)
    title = (a.get("title") or "Appointment").strip()
    if a.get("patient_name"):
        title = f"{title} ({a['patient_name']})"
    parts = []
    if a.get("doctor_name"):
        parts.append(f"Doctor: {a['doctor_name']}" + (f" ({a['specialty']})" if a.get("specialty") else ""))
    elif a.get("specialty"):
        parts.append(f"Specialty: {a['specialty']}")
    if a.get("kind"):
        parts.append(f"Kind: {a['kind']}")
    if a.get("notes"):
        parts.append(f"Notes: {a['notes']}")
    return start, end, title, "\n".join(parts), (a.get("location") or "").strip()


def google_calendar_url(a: dict) -> str:
    """Pre-filled Google Calendar event (works signed in on web and in the iOS app)."""
    start, end, title, details, location = _event_fields(a)
    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{ics_timestamp(start)}/{ics_timestamp(end)}",
        "details": details,
        "location": location,
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params, quote_via=quote)


def outlook_calendar_url(a: dict, work: bool = False) -> str:
    """Pre-filled Outlook.com (or Microsoft 365 when work=True) event."""
    start, end, title, details, location = _event_fields(a)
    host = "outlook.office.com" if work else "outlook.live.com"
    params = {
        "rru": "addevent",
        "subject": title,
        "startdt": start.replace(tzinfo=_dt.timezone.utc).isoformat(),
        "enddt": end.replace(tzinfo=_dt.timezone.utc).isoformat(),
        "body": details,
        "location": location,
        "path": "/calendar/action/compose",
    }
    return f"https://{host}/calendar/0/action/compose?" + urlencode(params, quote_via=quote)


def share_text(a: dict, tz_name: str | None = None) -> str:
    """Plain text for WhatsApp or SMS, times shown in tz_name (UTC when unknown)."""
    start, end, title, details, location = _event_fields(a)
    when = format_local(start, tz_name)
    lines = [f"{title}", f"When: {when}"]
    if location:
        lines.append(f"Where: {location}")
    if details:
        lines.append(details)
    return "\n".join(lines)


def whatsapp_share_url(text: str) -> str:
    return "https://wa.me/?text=" + quote(text)


def local_tz(tz_name: str | None):
    """A tzinfo for an IANA name, UTC when missing or unknown."""
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(tz_name) if tz_name else _dt.timezone.utc
    except Exception:
        return _dt.timezone.utc


def format_local(value: Any, tz_name: str | None, fmt: str = "%a %d %b %Y, %H:%M") -> str:
    """Render a stored UTC timestamp in the browser's timezone."""
    dt = to_naive_utc(value)
    if dt is None:
        return ""
    local = dt.replace(tzinfo=_dt.timezone.utc).astimezone(local_tz(tz_name))
    return local.strftime(fmt)


def from_local(day: _dt.date, time_of_day: _dt.time, tz_name: str | None) -> _dt.datetime:
    """Combine a wall-clock date and time in the browser's timezone into naive UTC for storage."""
    aware = _dt.datetime.combine(day, time_of_day).replace(tzinfo=local_tz(tz_name))
    return aware.astimezone(_dt.timezone.utc).replace(tzinfo=None)
