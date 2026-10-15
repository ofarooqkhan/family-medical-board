"""Appointments CRUD, reminders, and iCalendar export. Database tests run only
when TEST_DATABASE_URL points at a throwaway Postgres (fixtures in conftest.py);
the ICS tests need no database.
"""
import datetime as dt
import os

import pytest

import appointments as appts

TEST_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")


# iCalendar export (no database)

def _rows():
    return [
        {"id": 7, "title": "Cardiology, follow-up; BP check", "kind": "visit", "doctor_name": "Dr. Lee",
         "specialty": "Cardiology", "location": "Clinic A, Room 2", "starts_at": dt.datetime(2026, 11, 3, 14, 30),
         "ends_at": None, "reminder_minutes": 1440, "status": "scheduled", "notes": "Bring the BP log\nand the med list",
         "patient_name": "Parent One"},
        {"id": 8, "title": "MRI brain", "kind": "test", "doctor_name": None, "specialty": None, "location": None,
         "starts_at": "2026-11-10T09:00:00+02:00", "ends_at": "2026-11-10T10:00:00+02:00", "reminder_minutes": 60,
         "status": "scheduled", "notes": None},
    ]


def test_to_ics_structure_and_escaping():
    out = appts.to_ics(_rows(), calendar_name="Test Calendar")
    assert out.startswith("BEGIN:VCALENDAR\r\n")
    assert out.endswith("END:VCALENDAR\r\n")
    assert "\n" not in out.replace("\r\n", "")  # every line break is CRLF
    assert "VERSION:2.0" in out and "PRODID:" in out
    assert out.count("BEGIN:VEVENT") == 2 and out.count("END:VEVENT") == 2
    assert out.count("BEGIN:VALARM") == 2
    assert "TRIGGER:-PT1440M" in out and "TRIGGER:-PT60M" in out
    assert "UID:appointment-7@family-medical-board" in out
    # Naive timestamps are UTC; DTEND defaults to 30 minutes after start.
    assert "DTSTART:20261103T143000Z" in out and "DTEND:20261103T150000Z" in out
    # Aware timestamps are converted to UTC.
    assert "DTSTART:20261110T070000Z" in out and "DTEND:20261110T080000Z" in out
    assert "SUMMARY:Cardiology\\, follow-up\\; BP check" in out
    assert "LOCATION:Clinic A\\, Room 2" in out
    unfolded = out.replace("\r\n ", "")  # long lines are folded at 75 octets
    desc = [line for line in unfolded.split("\r\n") if line.startswith("DESCRIPTION:Patient")][0]
    assert "Doctor: Dr. Lee (Cardiology)" in desc
    assert "Notes: Bring the BP log\\nand the med list" in desc
    assert "DTSTAMP:" in out


def test_to_ics_folds_long_lines_and_handles_empty_input():
    empty = appts.to_ics([])
    assert "BEGIN:VEVENT" not in empty and empty.endswith("END:VCALENDAR\r\n")
    long_title = "Visit " + "x" * 200
    out = appts.to_ics([{"id": 1, "title": long_title, "starts_at": dt.datetime(2026, 1, 1, 8)}])
    for line in out.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    unfolded = out.replace("\r\n ", "")
    assert f"SUMMARY:{long_title}" in unfolded


def test_ics_escape_order():
    assert appts.ics_escape("a\\b;c,d\ne") == "a\\\\b\\;c\\,d\\ne"
    assert appts.ics_escape(None) == ""


# Database-backed tests

@pytest.fixture
def patient(accounts):
    import database
    return database.create_patient(accounts["editor"], "Parent One", age=82)


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


@needs_db
def test_appointment_crud_and_audit(accounts, patient):
    import database
    ed = accounts["editor"]
    start = _now() + dt.timedelta(days=3)
    aid = appts.add_appointment(patient, ed, "Cardiology", start, kind="visit", doctor_name="Dr. Lee",
                                specialty="Cardiology", location="Clinic A", notes="bring BP log")
    row = appts.get_appointment(aid, patient, ed)
    assert row["title"] == "Cardiology" and row["starts_at"] == start and row["ends_at"] is None
    assert row["reminder_minutes"] == 1440 and row["status"] == "scheduled" and row["created_by"] == ed["id"]

    appts.update_appointment(aid, patient, ed, title="Cardiology follow-up", ends_at=start + dt.timedelta(hours=1),
                             reminder_minutes=60, status="completed", created_by="nobody", bogus="x")
    row = appts.get_appointment(aid, patient, ed)
    assert row["title"] == "Cardiology follow-up" and row["ends_at"] == start + dt.timedelta(hours=1)
    assert row["reminder_minutes"] == 60 and row["status"] == "completed" and row["patient_id"] == patient

    # ISO strings are accepted for timestamps.
    aid2 = appts.add_appointment(patient, ed, "Labs", "2026-12-01T08:00:00Z", kind="test")
    assert appts.get_appointment(aid2, patient, ed)["starts_at"] == dt.datetime(2026, 12, 1, 8)

    appts.delete_appointment(aid, patient, ed)
    assert appts.get_appointment(aid, patient, ed) is None
    assert [r["id"] for r in appts.list_appointments(patient, ed)] == [aid2]
    with pytest.raises(PermissionError):
        appts.delete_appointment(aid, patient, ed)

    actions = [r["action"] for r in database.get_audit_log(patient)]
    assert {"appointment.add", "appointment.update", "appointment.delete"} <= set(actions)


@needs_db
def test_viewer_cannot_write(accounts, patient):
    ed, vw = accounts["editor"], accounts["viewer"]
    aid = appts.add_appointment(patient, ed, "Visit", _now() + dt.timedelta(days=1))
    with pytest.raises(PermissionError):
        appts.add_appointment(patient, vw, "Visit", _now())
    with pytest.raises(PermissionError):
        appts.update_appointment(aid, patient, vw, title="x")
    with pytest.raises(PermissionError):
        appts.delete_appointment(aid, patient, vw)
    assert len(appts.list_appointments(patient, vw)) == 1
    assert appts.get_appointment(aid, patient, vw)["title"] == "Visit"


@needs_db
def test_list_order_and_upcoming_filter(accounts, patient):
    ed = accounts["editor"]
    now = _now()
    past = appts.add_appointment(patient, ed, "Past", now - dt.timedelta(days=2))
    soon = appts.add_appointment(patient, ed, "Soon", now + dt.timedelta(hours=2))
    later = appts.add_appointment(patient, ed, "Later", now + dt.timedelta(days=5))
    cancelled = appts.add_appointment(patient, ed, "Cancelled", now + dt.timedelta(days=1))
    appts.update_appointment(cancelled, patient, ed, status="cancelled")

    assert [r["id"] for r in appts.list_appointments(patient, ed)] == [past, soon, cancelled, later]
    assert [r["id"] for r in appts.list_appointments(patient, ed, upcoming_only=True)] == [soon, later]
    assert [r["id"] for r in appts.list_appointments(patient, ed, limit=2)] == [past, soon]


@needs_db
def test_due_reminders_window(accounts, patient):
    import database
    ed, admin, vw = accounts["editor"], accounts["admin"], accounts["viewer"]
    now = _now()
    due = appts.add_appointment(patient, ed, "Due", now + dt.timedelta(minutes=60), reminder_minutes=120)
    appts.add_appointment(patient, ed, "Not yet", now + dt.timedelta(hours=5), reminder_minutes=60)
    appts.add_appointment(patient, ed, "Already started", now - dt.timedelta(minutes=5), reminder_minutes=120)
    done = appts.add_appointment(patient, ed, "Completed", now + dt.timedelta(minutes=30), reminder_minutes=120)
    appts.update_appointment(done, patient, ed, status="completed")
    edge = appts.add_appointment(patient, ed, "Edge", now + dt.timedelta(minutes=120), reminder_minutes=120)

    other = database.create_patient(ed, "Parent Two")
    other_due = appts.add_appointment(other, ed, "Other due", now + dt.timedelta(minutes=10), reminder_minutes=30)
    archived = database.create_patient(ed, "Archived")
    appts.add_appointment(archived, ed, "Hidden", now + dt.timedelta(minutes=10), reminder_minutes=30)
    database.archive_patient(archived, admin)

    rows = appts.due_reminders(vw, now=now)
    assert [(r["id"], r["patient_name"]) for r in rows] == [(other_due, "Parent Two"), (due, "Parent One"), (edge, "Parent One")]
    # Just before the window opens the edge case drops out; once started it is gone.
    assert edge not in {r["id"] for r in appts.due_reminders(vw, now=now - dt.timedelta(seconds=1))}
    assert due not in {r["id"] for r in appts.due_reminders(vw, now=now + dt.timedelta(minutes=60))}
    # Aware "now" values are converted to UTC.
    aware = now.replace(tzinfo=dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=3)))
    assert [r["id"] for r in appts.due_reminders(vw, now=aware)] == [r["id"] for r in rows]


@needs_db
def test_cross_patient_scoping(accounts):
    import database
    ed = accounts["editor"]
    a = database.create_patient(ed, "Parent A")
    b = database.create_patient(ed, "Parent B")
    aid = appts.add_appointment(a, ed, "Visit", _now() + dt.timedelta(days=1))
    assert appts.list_appointments(b, ed) == []
    assert appts.get_appointment(aid, b, ed) is None
    with pytest.raises(PermissionError):
        appts.update_appointment(aid, b, ed, title="hijack")
    with pytest.raises(PermissionError):
        appts.delete_appointment(aid, b, ed)
    assert appts.get_appointment(aid, a, ed)["title"] == "Visit"


@needs_db
def test_to_ics_from_database_rows(accounts, patient):
    ed = accounts["editor"]
    appts.add_appointment(patient, ed, "Visit, one", dt.datetime(2026, 11, 3, 14, 30), location="Clinic A")
    appts.add_appointment(patient, ed, "Visit two", dt.datetime(2026, 11, 4, 9, 0), ends_at=dt.datetime(2026, 11, 4, 9, 45))
    out = appts.to_ics(appts.list_appointments(patient, ed))
    assert out.count("BEGIN:VEVENT") == 2 and out.count("BEGIN:VALARM") == 2
    assert "SUMMARY:Visit\\, one" in out
    assert "DTEND:20261104T094500Z" in out


# Add-to-calendar links and browser timezone helpers (no database)

def test_google_and_outlook_links_carry_utc_times_and_details():
    a = _rows()[0]
    g = appts.google_calendar_url(a)
    assert g.startswith("https://calendar.google.com/calendar/render?action=TEMPLATE")
    assert "dates=20261103T143000Z%2F20261103T150000Z" in g
    assert "Parent%20One" in g and "Dr.%20Lee" in g and "Clinic%20A" in g
    o = appts.outlook_calendar_url(a)
    assert o.startswith("https://outlook.live.com/calendar/0/action/compose?rru=addevent")
    assert "startdt=2026-11-03T14%3A30%3A00%2B00%3A00" in o and "enddt=2026-11-03T15%3A00%3A00%2B00%3A00" in o
    assert appts.outlook_calendar_url(a, work=True).startswith("https://outlook.office.com/")


def test_share_text_uses_the_browser_timezone():
    a = _rows()[0]
    text = appts.share_text(a, "Asia/Karachi")
    assert "Tue 03 Nov 2026, 19:30" in text and "Clinic A" in text
    assert appts.whatsapp_share_url(text).startswith("https://wa.me/?text=Cardiology")
    assert "Tue 03 Nov 2026, 14:30" in appts.share_text(a, "Nowhere/Invalid")


def test_from_local_and_format_local_round_trip():
    stored = appts.from_local(dt.date(2026, 11, 3), dt.time(19, 30), "Asia/Karachi")
    assert stored == dt.datetime(2026, 11, 3, 14, 30)
    assert appts.format_local(stored, "Asia/Karachi") == "Tue 03 Nov 2026, 19:30"
    assert appts.format_local(stored, None) == "Tue 03 Nov 2026, 14:30"
    assert appts.format_local(None, "Asia/Karachi") == ""
