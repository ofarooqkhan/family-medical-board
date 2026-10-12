"""Medication list, history, duplicates, and reconciliation. Database tests run
only when TEST_DATABASE_URL points at a throwaway Postgres (fixtures in conftest.py).
"""
import datetime as dt
import os

import pytest

import medications as meds

TEST_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")


# Pure helpers (no database)

def test_normalize_name_strips_case_space_and_punctuation():
    assert meds.normalize_name("  Metformin HCl 500-mg ") == "metforminhcl500mg"
    assert meds.normalize_name(None) == ""


def test_parse_date_accepts_iso_strings_dates_and_junk():
    assert meds.parse_date("2026-01-15") == dt.date(2026, 1, 15)
    assert meds.parse_date("2026-01-15T10:30:00") == dt.date(2026, 1, 15)
    assert meds.parse_date(dt.date(2026, 2, 1)) == dt.date(2026, 2, 1)
    assert meds.parse_date(dt.datetime(2026, 2, 1, 9)) == dt.date(2026, 2, 1)
    assert meds.parse_date("last Tuesday") is None
    assert meds.parse_date("") is None
    assert meds.parse_date(None) is None


# Database-backed tests

@pytest.fixture
def patient(accounts):
    import database
    return database.create_patient(accounts["editor"], "Parent One", age=82)


@needs_db
def test_add_then_update_keeps_two_history_rows(accounts, patient):
    ed = accounts["editor"]
    mid = meds.add_medication(patient, ed, "Metformin", dose="500 mg", route="oral", frequency="twice daily",
                              prescriber="Dr. Patel", start_date="2026-01-15")
    meds.update_medication(mid, patient, ed, dose="1000 mg", frequency="once daily", ignored_field="x")

    rows = meds.list_medications(patient, ed)
    assert len(rows) == 1
    assert rows[0]["dose"] == "1000 mg" and rows[0]["start_date"] == dt.date(2026, 1, 15)

    history = meds.medication_history(patient, ed, medication_id=mid)
    assert [h["change_type"] for h in history] == ["changed", "added"]
    changed, added = history
    assert added["before"] is None
    assert added["after"]["name"] == "Metformin" and added["after"]["dose"] == "500 mg"
    assert added["after"]["start_date"] == "2026-01-15"
    assert changed["before"]["dose"] == "500 mg" and changed["after"]["dose"] == "1000 mg"
    assert changed["before"]["frequency"] == "twice daily" and changed["after"]["frequency"] == "once daily"
    assert changed["before"]["name"] == changed["after"]["name"] == "Metformin"
    assert all(h["medication_name"] == "Metformin" for h in history)
    assert all(h["changed_by_name"] == ed["display_name"] for h in history)
    assert meds.medication_history(patient, ed) == history

    import database
    actions = [r["action"] for r in database.get_audit_log(patient)]
    assert {"medication.add", "medication.update"} <= set(actions)


@needs_db
def test_stop_sets_status_end_date_and_reason(accounts, patient):
    ed = accounts["editor"]
    mid = meds.add_medication(patient, ed, "Lisinopril", dose="10 mg", notes="morning")
    meds.stop_medication(mid, patient, ed, reason="dizziness")
    row = meds.get_medication(mid, patient, ed)
    assert row["status"] == "stopped"
    assert row["end_date"] == dt.date.today()
    assert row["notes"].startswith("morning\n") and "dizziness" in row["notes"]
    assert meds.medication_history(patient, ed, medication_id=mid)[0]["change_type"] == "stopped"
    assert meds.list_medications(patient, ed, status="active") == []
    assert [r["id"] for r in meds.list_medications(patient, ed, status="stopped")] == [mid]

    mid2 = meds.add_medication(patient, ed, "Aspirin")
    meds.stop_medication(mid2, patient, ed, end_date="2026-03-01")
    assert meds.get_medication(mid2, patient, ed)["end_date"] == dt.date(2026, 3, 1)
    # Setting status through update_medication is also recorded as a stop.
    mid3 = meds.add_medication(patient, ed, "Atorvastatin")
    meds.update_medication(mid3, patient, ed, status="stopped")
    assert meds.medication_history(patient, ed, medication_id=mid3)[0]["change_type"] == "stopped"

    import database
    assert "medication.stop" in {r["action"] for r in database.get_audit_log(patient)}


@needs_db
def test_list_is_ordered_by_status_then_name(accounts, patient):
    ed = accounts["editor"]
    a = meds.add_medication(patient, ed, "Zinc")
    meds.add_medication(patient, ed, "Aspirin")
    meds.stop_medication(a, patient, ed)
    meds.add_medication(patient, ed, "Metformin")
    assert [(r["status"], r["name"]) for r in meds.list_medications(patient, ed)] == [
        ("active", "Aspirin"), ("active", "Metformin"), ("stopped", "Zinc")]


@needs_db
def test_viewer_cannot_write_but_can_read(accounts, patient):
    ed, vw = accounts["editor"], accounts["viewer"]
    mid = meds.add_medication(patient, ed, "Metformin")
    with pytest.raises(PermissionError):
        meds.add_medication(patient, vw, "Aspirin")
    with pytest.raises(PermissionError):
        meds.update_medication(mid, patient, vw, dose="1 mg")
    with pytest.raises(PermissionError):
        meds.stop_medication(mid, patient, vw)
    with pytest.raises(PermissionError):
        meds.reconcile_from_items(patient, vw, [{"payload": {"name": "Aspirin"}}])
    assert len(meds.list_medications(patient, vw)) == 1
    assert len(meds.medication_history(patient, vw)) == 1
    assert meds.find_duplicates(patient, vw) == {"duplicates": [], "multi_prescriber": []}


@needs_db
def test_duplicates_and_multi_prescriber(accounts, patient):
    ed = accounts["editor"]
    m1 = meds.add_medication(patient, ed, "Metformin", prescriber="Dr. Patel")
    m2 = meds.add_medication(patient, ed, "METFORMIN 500", generic_name="metformin", prescriber="Dr. Lee")
    m3 = meds.add_medication(patient, ed, "Glucophage", generic_name="Metformin", prescriber="Dr. Patel")
    m4 = meds.add_medication(patient, ed, "Lipitor", generic_name="atorvastatin", prescriber="Dr. Patel")
    m5 = meds.add_medication(patient, ed, "Atorvastatin", prescriber="Dr. Patel")
    meds.add_medication(patient, ed, "Aspirin", prescriber="Dr. Lee")
    old = meds.add_medication(patient, ed, "Aspirin", prescriber="Dr. Patel")
    meds.stop_medication(old, patient, ed)  # stopped rows are ignored

    result = meds.find_duplicates(patient, ed)
    groups = sorted(sorted(r["id"] for r in g) for g in result["duplicates"])
    # Metformin x2 share a normalized name; Glucophage joins through generic_name.
    # Lipitor's generic matches Atorvastatin's name. The stopped Aspirin does not count.
    assert groups == [[m1, m2, m3], [m4, m5]]
    multi = result["multi_prescriber"]
    assert len(multi) == 1
    assert multi[0]["name"] == "metformin"
    assert multi[0]["prescribers"] == ["Dr. Lee", "Dr. Patel"]
    assert sorted(r["id"] for r in multi[0]["medications"]) == [m1, m2, m3]


@needs_db
def test_reconcile_adds_updates_and_leaves_unchanged(accounts, patient):
    import database
    ed = accounts["editor"]
    existing = meds.add_medication(patient, ed, "Metformin", dose="500 mg", frequency="twice daily", prescriber="Dr. Patel")
    same = meds.add_medication(patient, ed, "Lisinopril", dose="10 mg")
    fid = database.save_document(patient, ed, "rx.pdf", "application/pdf", b"%PDF", "prescription",
                                 pages=[{"page_number": 1, "image_png": b"P", "width": 1, "height": 1}])
    ids = database.add_extracted_items(patient, fid, ed, [
        {"page_number": 1, "item_type": "medication",
         "payload": {"name": "metformin", "dose": "1000 mg", "frequency": "", "prescriber": None, "start_date": "2026-02-01"}},
        {"page_number": 1, "item_type": "medication", "payload": {"name": "Lisinopril", "dose": " 10 mg "}},
        {"page_number": 1, "item_type": "medication",
         "payload": {"name": "Atorvastatin", "dose": "20 mg", "route": "oral", "start_date": "not a date", "generic_name": "atorvastatin"}},
        {"page_number": 1, "item_type": "lab_value", "payload": {"name": "glucose"}},
    ])
    items = database.list_extracted_items(patient, ed, file_id=fid)
    assert len(items) == 4

    result = meds.reconcile_from_items(patient, ed, items)
    assert result["updated"] == [existing]
    assert result["unchanged"] == [same]
    assert len(result["added"]) == 1
    new_id = result["added"][0]

    updated = meds.get_medication(existing, patient, ed)
    assert updated["dose"] == "1000 mg"
    assert updated["start_date"] == dt.date(2026, 2, 1)
    assert updated["frequency"] == "twice daily" and updated["prescriber"] == "Dr. Patel"  # empties do not overwrite
    assert updated["source_file_id"] == fid and updated["source_page"] == 1

    added = meds.get_medication(new_id, patient, ed)
    assert added["name"] == "Atorvastatin" and added["dose"] == "20 mg" and added["route"] == "oral"
    assert added["start_date"] is None
    assert added["source_file_id"] == fid and added["extracted_item_id"] == ids[2]

    history = meds.medication_history(patient, ed)
    assert [h["change_type"] for h in history if h["medication_id"] == existing] == ["changed", "added"]
    assert [h["change_type"] for h in history if h["medication_id"] == new_id] == ["added"]
    assert [h["change_type"] for h in history if h["medication_id"] == same] == ["added"]
    assert [h["source_file_id"] for h in history if h["medication_id"] == existing][0] == fid

    # Running the same items again changes nothing.
    again = meds.reconcile_from_items(patient, ed, items)
    assert again["added"] == [] and again["updated"] == []
    assert sorted(again["unchanged"]) == sorted([existing, same, new_id])
    assert len(meds.medication_history(patient, ed)) == len(history)


@needs_db
def test_cross_patient_scoping(accounts):
    import database
    ed = accounts["editor"]
    a = database.create_patient(ed, "Parent A")
    b = database.create_patient(ed, "Parent B")
    mid = meds.add_medication(a, ed, "Metformin", prescriber="Dr. Patel")
    meds.add_medication(a, ed, "Metformin", prescriber="Dr. Lee")

    assert meds.list_medications(b, ed) == []
    assert meds.get_medication(mid, b, ed) is None
    assert meds.medication_history(b, ed) == []
    assert meds.medication_history(b, ed, medication_id=mid) == []
    assert meds.find_duplicates(b, ed) == {"duplicates": [], "multi_prescriber": []}
    with pytest.raises(PermissionError):
        meds.update_medication(mid, b, ed, dose="1 mg")
    with pytest.raises(PermissionError):
        meds.stop_medication(mid, b, ed)
    # Reconciling through B adds a fresh row for B instead of touching A's.
    result = meds.reconcile_from_items(b, ed, [{"payload": {"name": "Metformin", "dose": "1 mg"}}])
    assert len(result["added"]) == 1 and result["updated"] == []
    assert meds.get_medication(mid, a, ed)["dose"] is None
    assert len(meds.list_medications(a, ed)) == 2
    assert len(meds.list_medications(b, ed)) == 1
    assert all(r["patient_id"] == a for r in database.get_audit_log(a) if r["entity"] == "medication")
