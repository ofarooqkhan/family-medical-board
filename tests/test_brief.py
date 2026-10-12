import os
from datetime import datetime, timedelta

import pytest

import brief

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


def test_brief_collects_everything_and_renders(accounts):
    import appointments
    import database as db
    import medications
    ed = accounts["editor"]
    pid = db.create_patient(ed, "Parent One", age=82, allergies="penicillin")
    mid = medications.add_medication(pid, ed, "Metformin", dose="500 mg", frequency="twice daily", prescriber="Dr. Khan")
    medications.update_medication(mid, pid, ed, dose="850 mg")
    db.save_vital_reading(pid, ed, "lab_hba1c", 7.8, "%", reference_range="4.0-5.6", flag="high")
    aid = appointments.add_appointment(pid, ed, "Diabetes follow-up", datetime.now() + timedelta(days=3),
                                       doctor_name="Dr. Khan", specialty="Endocrinology", location="City Clinic")
    with db.db_cursor(commit=True) as cur:
        cur.execute("""INSERT INTO board_runs (patient_id, user_id, query, routed_agents, specialist_outputs, coordinator_output, model_calls)
                       VALUES (%s, %s, 'q', '{}', '[]', %s, '[]')""",
                    (pid, ed["id"], '{"headline": "h", "what_changed": [], "key_observations": [], "missing_records": ["No ECG on file"], '
                                    '"disagreements": [], "declined_advice_request": false, "decline_note": null, '
                                    '"questions_by_doctor": [{"doctor": "Endocrinologist", "questions": ["Is the HbA1c trend expected?", "Is the HbA1c trend expected?"]}, '
                                    '{"doctor": "Cardiologist", "questions": ["ECG needed?"]}]}'))
    b = brief.build_brief(pid, accounts["viewer"], appointment_id=aid)
    assert b["patient"]["name"] == "Parent One" and b["appointment"]["id"] == aid
    assert [m["dose"] for m in b["medications"]] == ["850 mg"]
    assert {c["change_type"] for c in b["medication_changes"]} == {"added", "changed"}
    assert b["values"][0]["flag"] == "high"
    # Only the endocrinology question, de-duplicated; the cardiology one is for another visit.
    assert [q["question"] for q in b["questions"]] == ["Is the HbA1c trend expected?"]
    assert b["missing_records"] == ["No ECG on file"]

    md = brief.brief_markdown(b, "en")
    assert "Pre-visit brief: Parent One" in md and "Metformin 850 mg" in md and "Is the HbA1c trend expected?" in md
    assert "not medical advice" in md
    ur = brief.brief_markdown(b, "ur")
    assert "ملاقات سے پہلے کا خلاصہ" in ur and "Metformin" in ur

    pdf = brief.brief_pdf(b, "en")
    assert pdf[:5] == b"%PDF-" and len(pdf) > 1000
    pdf_ur = brief.brief_pdf(b, "ur")
    assert pdf_ur[:5] == b"%PDF-"

    # Without an appointment every question is included.
    b_all = brief.build_brief(pid, ed)
    assert len(b_all["questions"]) == 2 and b_all["appointment"] is None
