"""Upload, extract, review, confirm. Database tests."""
import io
import os

import pytest

import documents
import extraction
from extraction import DocumentClassification, LabReportDocument, PrescriptionDocument
from providers import FakeProvider

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


def make_pdf(texts):
    import pymupdf
    doc = pymupdf.open()
    for t in texts:
        doc.new_page().insert_text((72, 72), t)
    return doc.tobytes()


def make_png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(buf, format="PNG")
    return buf.getvalue()


RX = {
    "prescriber": "Dr. Khan", "clinic": None, "patient_name": None, "document_date": "2026-09-01", "diagnoses": [],
    "notes": None, "overall_confidence": 0.9,
    "medications": [
        {"name": "Metformin", "generic_name": "metformin", "dose": "500 mg", "route": "oral", "frequency": "twice daily",
         "duration": None, "instructions": "after meals", "page": 1, "confidence": 0.95, "low_confidence_fields": []},
        {"name": "Amlod?ne", "generic_name": None, "dose": "5 mg", "route": None, "frequency": "once daily", "duration": None,
         "instructions": None, "page": 2, "confidence": 0.4, "low_confidence_fields": ["name"]},
    ],
}
LAB = {
    "lab_name": "City Lab", "ordering_doctor": None, "patient_name": None, "document_date": "2026-09-05", "comments": None,
    "overall_confidence": 0.9,
    "values": [{"test_name": "HbA1c", "value_text": "7.8", "numeric_value": 7.8, "unit": "%", "reference_range": "4.0-5.6",
                "flag": "high", "collected_at": None, "page": 1, "confidence": 0.9, "low_confidence_fields": []},
               {"test_name": "Culture", "value_text": "No growth", "numeric_value": None, "unit": None, "reference_range": None,
                "flag": "normal", "collected_at": None, "page": 1, "confidence": 0.7, "low_confidence_fields": []}],
}


def _patient(accounts):
    import database as db
    return db.create_patient(accounts["editor"], "Parent One", age=80)


def test_upload_without_provider_stores_pages_only(accounts):
    import database as db
    pid = _patient(accounts)
    out = documents.ingest_upload(pid, accounts["editor"], "rx.pdf", "application/pdf", make_pdf(["Rx one", "two"]), provider=None)
    assert out["pages"] == 2 and out["items"] == 0
    doc = db.get_document(out["file_id"], pid, accounts["viewer"])
    assert doc["status"] == "uploaded" and doc["page_count"] == 2 and "Rx one" in doc["extracted_text"]
    pages = db.get_document_pages(out["file_id"], pid, accounts["viewer"])
    assert len(pages) == 2 and bytes(pages[0]["image_png"])[:3] == b"\xff\xd8\xff"


def test_viewer_cannot_upload(accounts):
    pid = _patient(accounts)
    with pytest.raises(PermissionError):
        documents.ingest_upload(pid, accounts["viewer"], "x.png", "image/png", make_png(), provider=None)


def test_prescription_flow_confirm_and_reject(accounts):
    import database as db
    import medications
    ed = accounts["editor"]
    pid = _patient(accounts)
    fake = FakeProvider(responses={
        DocumentClassification: {"kind": "prescription", "confidence": 0.9, "patient_name": None, "document_date": None,
                                 "language": "en", "reason": "rx"},
        PrescriptionDocument: RX,
    })
    out = documents.ingest_upload(pid, ed, "rx.pdf", "application/pdf", make_pdf(["Rx", "p2"]), provider=fake)
    assert out["kind"] == "prescription" and out["items"] == 2 and out["error"] is None
    assert [c["purpose"] for c in out["calls"]] == ["classify", "extract:prescription"]
    doc = db.get_document(out["file_id"], pid, ed)
    assert doc["status"] == "extracted" and doc["document_kind"] == "prescription"
    assert str(doc["document_date"]) == "2026-09-01" and doc["extraction_model"]

    # Nothing is in the medication list until the family confirms.
    assert medications.list_medications(pid, ed) == []
    pending = documents.pending_review(pid, ed)
    items = pending[out["file_id"]]
    assert [i["page_number"] for i in items] == [1, 2]

    # Confirm the first as is, correct the second's name, then confirm it.
    r1 = documents.confirm_item(items[0]["id"], pid, ed, items[0]["payload"])
    corrected = dict(items[1]["payload"], name="Amlodipine")
    r2 = documents.confirm_item(items[1]["id"], pid, ed, corrected)
    meds = {m["name"]: m for m in medications.list_medications(pid, ed, status="active")}
    assert set(meds) == {"Metformin", "Amlodipine"}
    assert meds["Metformin"]["prescriber"] == "Dr. Khan" and str(meds["Metformin"]["start_date"]) == "2026-09-01"
    assert meds["Metformin"]["source_file_id"] == out["file_id"] and meds["Metformin"]["source_page"] == 1
    assert meds["Amlodipine"]["source_page"] == 2 and meds["Amlodipine"]["extracted_item_id"] == items[1]["id"]
    assert r1["medication_id"] == meds["Metformin"]["id"] and r2["medication_id"] == meds["Amlodipine"]["id"]
    # Document is reviewed once no item is pending; confirming twice is a no-op.
    assert db.get_document(out["file_id"], pid, ed)["status"] == "reviewed"
    assert documents.confirm_item(items[0]["id"], pid, ed, items[0]["payload"]).get("already") is True
    assert len(medications.list_medications(pid, ed)) == 2
    stored = {i["id"]: i for i in db.list_extracted_items(pid, ed)}
    assert stored[items[1]["id"]]["payload"]["name"] == "Amlodipine"


def test_lab_flow_writes_vitals_with_source_links(accounts):
    import database as db
    ed = accounts["editor"]
    pid = _patient(accounts)
    fake = FakeProvider(responses={LabReportDocument: LAB})
    out = documents.ingest_upload(pid, ed, "lab.png", "image/png", make_png(), provider=fake, kind="lab_report")
    assert [c["purpose"] for c in out["calls"]] == ["extract:lab_report"]
    items = documents.pending_review(pid, ed)[out["file_id"]]
    hba1c = next(i for i in items if i["payload"]["test_name"] == "HbA1c")
    culture = next(i for i in items if i["payload"]["test_name"] == "Culture")
    res = documents.confirm_item(hba1c["id"], pid, ed, hba1c["payload"])
    row = db.get_vitals_history(pid, ed, reading_type="lab_hba1c")[0]
    assert row["id"] == res["vital_id"] and row["value"] == pytest.approx(7.8) and row["unit"] == "%"
    assert row["reference_range"] == "4.0-5.6" and row["flag"] == "high"
    assert str(row["recorded_at"])[:10] == "2026-09-05"
    assert row["source_file_id"] == out["file_id"] and row["source_page"] == 1 and row["extracted_item_id"] == hba1c["id"]
    with pytest.raises(ValueError):
        documents.confirm_item(culture["id"], pid, ed, culture["payload"])  # no numeric value
    documents.reject_item(culture["id"], pid, ed)
    assert documents.pending_review(pid, ed) == {}
    assert db.get_document(out["file_id"], pid, ed)["status"] == "reviewed"


def test_extraction_failure_is_recorded_not_raised(accounts):
    import database as db
    ed = accounts["editor"]
    pid = _patient(accounts)
    out = documents.ingest_upload(pid, ed, "lab.png", "image/png", make_png(), provider=FakeProvider(), kind="lab_report")
    assert out["error"] and out["items"] == 0
    doc = db.get_document(out["file_id"], pid, ed)
    assert doc["status"] == "failed" and "FakeProvider" in doc["extraction_error"]
    # Re-running with a working provider replaces the result.
    out2 = documents.run_extraction(pid, out["file_id"], ed, FakeProvider(responses={LabReportDocument: LAB}), kind="lab_report")
    assert out2["items"] == 2 and db.get_document(out["file_id"], pid, ed)["status"] == "extracted"


def test_rerun_discards_old_pending_items(accounts):
    ed = accounts["editor"]
    pid = _patient(accounts)
    fake = FakeProvider(responses={LabReportDocument: LAB})
    out = documents.ingest_upload(pid, ed, "lab.png", "image/png", make_png(), provider=fake, kind="lab_report")
    documents.run_extraction(pid, out["file_id"], ed, fake, kind="lab_report")
    pending = documents.pending_review(pid, ed)[out["file_id"]]
    assert len(pending) == 2


def test_confirm_is_scoped_by_patient(accounts):
    import database as db
    ed = accounts["editor"]
    a = _patient(accounts)
    b = db.create_patient(ed, "Parent B")
    out = documents.ingest_upload(a, ed, "lab.png", "image/png", make_png(), provider=FakeProvider(responses={LabReportDocument: LAB}), kind="lab_report")
    item = documents.pending_review(a, ed)[out["file_id"]][0]
    with pytest.raises(PermissionError):
        documents.confirm_item(item["id"], b, ed, item["payload"])
    with pytest.raises(PermissionError):
        documents.confirm_item(item["id"], a, accounts["viewer"], item["payload"])


LOG = {
    "patient_name": None, "device": "Omron", "overall_confidence": 0.9,
    "readings": [
        {"kind": "blood_pressure", "name": None, "value": None, "systolic": 142, "diastolic": 88, "pulse": 71, "unit": "mmHg",
         "recorded_at": "2026-10-01 08:00", "notes": "morning", "page": 1, "confidence": 0.95, "low_confidence_fields": []},
        {"kind": "glucose", "name": None, "value": 150, "systolic": None, "diastolic": None, "pulse": None, "unit": "mg/dL",
         "recorded_at": "2026-10-02", "notes": "fasting", "page": 1, "confidence": 0.9, "low_confidence_fields": []},
        {"kind": "weight", "name": None, "value": 71, "systolic": None, "diastolic": None, "pulse": None, "unit": "kg",
         "recorded_at": None, "notes": None, "page": 1, "confidence": 0.8, "low_confidence_fields": ["recorded_at"]},
    ],
}


def test_readings_log_photo_and_typed_notes_flow(accounts):
    import database as db
    from extraction import VitalsLogDocument
    ed = accounts["editor"]
    pid = _patient(accounts)
    fake = FakeProvider(responses={VitalsLogDocument: LOG})
    out = documents.ingest_upload(pid, ed, "bp-diary.png", "image/png", make_png(), provider=fake, kind="vitals_log")
    assert out["items"] == 3
    items = documents.pending_review(pid, ed)[out["file_id"]]
    assert [i["item_type"] for i in items] == ["vital", "vital", "vital"]
    res = documents.confirm_item(items[0]["id"], pid, ed, items[0]["payload"])
    assert len(res["vital_ids"]) == 3  # systolic, diastolic, pulse
    documents.confirm_item(items[1]["id"], pid, ed, items[1]["payload"])
    documents.confirm_item(items[2]["id"], pid, ed, items[2]["payload"])
    latest = {v["reading_type"]: v for v in db.get_latest_vitals(pid, ed)}
    assert latest["blood_pressure_systolic"]["value"] == 142 and str(latest["blood_pressure_systolic"]["recorded_at"]) == "2026-10-01 08:00:00"
    assert latest["heart_rate"]["value"] == 71 and latest["glucose"]["value"] == 150 and latest["weight"]["unit"] == "kg"
    assert latest["weight"]["recorded_at"] is None
    assert latest["glucose"]["source_file_id"] == out["file_id"]

    # Typed notes go through the same review step, stored as a text document.
    out2 = documents.stage_text_readings(pid, ed, "BP 142/88 pulse 71 on 1 Oct morning", fake)
    assert out2["items"] == 3 and fake.calls[-1]["purpose"] == "extract:text_readings"
    doc = db.get_document(out2["file_id"], pid, ed)
    assert doc["document_kind"] == "vitals_log" and doc["mime_type"] == "text/plain"
    assert len(documents.pending_review(pid, ed)[out2["file_id"]]) == 3


def test_auto_add_confident_items_and_hold_unsure_ones(accounts):
    """Owner's rule: items the model is sure about join the record on their
    own; the upload ends with a summary of what was added and what needs a look."""
    import database as db
    import medications
    ed = accounts["editor"]
    pid = _patient(accounts)
    rx = dict(RX)
    rx["medications"] = [
        dict(RX["medications"][0], name="Metformin", confidence=0.95, low_confidence_fields=[]),
        dict(RX["medications"][0], name="Amlodipine", confidence=0.9, low_confidence_fields=["dose"]),
        dict(RX["medications"][0], name="Unclear", confidence=0.4, low_confidence_fields=[]),
    ]
    fake = FakeProvider(responses={PrescriptionDocument: rx})
    out = documents.ingest_upload(pid, ed, "rx.pdf", "application/pdf", make_pdf(["Rx"]), fake, kind="prescription", auto_add=True)
    assert out["items"] == 3
    assert [a.split(" ")[0] for a in out["added"]] == ["Metformin"]
    assert sorted(a.split(" ")[0] for a in out["to_check"]) == ["Amlodipine", "Unclear"]
    names = [m["name"] for m in medications.list_medications(pid, ed, status="active")]
    assert names == ["Metformin"]
    pending = documents.pending_review(pid, ed)[out["file_id"]]
    assert sorted(i["payload"]["name"] for i in pending) == ["Amlodipine", "Unclear"]
    assert any(r["action"] == "medication.add" for r in db.get_audit_log(pid))
    # With auto-add off (the environment switch), everything waits.
    out2 = documents.ingest_upload(pid, ed, "rx2.pdf", "application/pdf", make_pdf(["Rx two"]), fake, kind="prescription", auto_add=False)
    assert out2["added"] == [] and len(out2["to_check"]) == 3


def test_item_labels_and_confirmable():
    assert documents.item_label({"item_type": "medication", "payload": {"name": "Metformin", "dose": "500 mg", "frequency": "twice daily"}}) == "Metformin 500 mg twice daily"
    assert documents.item_label({"item_type": "lab_value", "payload": {"test_name": "HbA1c", "numeric_value": 7.8, "unit": "%"}}) == "HbA1c 7.8 %"
    assert documents.confirmable({"item_type": "lab_value", "payload": {"numeric_value": None}}) is False
    assert documents.confirmable({"item_type": "medication", "payload": {"name": "x"}}) is True
    assert documents.confirmable({"item_type": "appointment", "payload": {}}) is False


def test_one_collated_memory_note_per_document_dated_by_the_report(accounts):
    """Owner's rule: a report is one thing in memory, placed by its own date,
    not a scatter of per-item notes."""
    import database as db
    import memory
    ed = accounts["editor"]
    pid = _patient(accounts)
    lab = {"lab_name": "Chughtai Lab", "ordering_doctor": "Dr. Khan", "patient_name": None, "document_date": "2026-09-05",
           "comments": None, "overall_confidence": 0.9,
           "values": [{"test_name": "HbA1c", "value_text": "7.8", "numeric_value": 7.8, "unit": "%", "reference_range": "4.0-5.6",
                       "flag": "high", "collected_at": None, "page": 1, "confidence": 0.95, "low_confidence_fields": []},
                      {"test_name": "Creatinine", "value_text": "1.3", "numeric_value": 1.3, "unit": "mg/dL", "reference_range": "0.6-1.2",
                       "flag": "high", "collected_at": None, "page": 1, "confidence": 0.9, "low_confidence_fields": []}]}
    fake = FakeProvider(responses={LabReportDocument: lab})
    out = documents.ingest_upload(pid, ed, "lab.pdf", "application/pdf", make_pdf(["Lab"]), fake, kind="lab_report", auto_add=True)
    notes = [m for m in db.list_doctor_memories(pid, ed) if m["source_file_id"] == out["file_id"]]
    assert len(notes) == 1, [n["content"] for n in notes]
    n = notes[0]
    assert n["memory_type"] == "document" and n["doctor_type"] == "general_duty" and n["status"] == "active"
    assert str(n["event_date"]) == "2026-09-05"
    assert n["content"].startswith("Lab report, 2026-09-05, from Chughtai Lab")
    assert "HbA1c 7.8 % (high)" in n["content"] and "Creatinine 1.3 mg/dL (high)" in n["content"]
    assert out["memory_id"] == n["id"]
    # The specialist sees it under the report's date.
    block = memory.notes_block(pid, ed, "general_duty", "kidney")
    assert "(2026-09-05)" in block and "Creatinine" in block
    # Reading the document again replaces the note instead of adding a second one.
    documents.run_extraction(pid, out["file_id"], ed, fake, kind="lab_report", auto_add=True)
    all_notes = [m for m in db.list_doctor_memories(pid, ed) if m["source_file_id"] == out["file_id"]]
    assert len(all_notes) == 2 and sorted(m["status"] for m in all_notes) == ["active", "superseded"]
    assert [m for m in all_notes if m["status"] == "superseded"][0]["superseded_by"] == [m for m in all_notes if m["status"] == "active"][0]["id"]
    # A prescription goes to the pharmacist with its medicines in one line.
    fake_rx = FakeProvider(responses={PrescriptionDocument: RX})
    out2 = documents.ingest_upload(pid, ed, "rx.pdf", "application/pdf", make_pdf(["Rx"]), fake_rx, kind="prescription", auto_add=True)
    rx_note = [m for m in db.list_doctor_memories(pid, ed) if m["source_file_id"] == out2["file_id"]]
    assert len(rx_note) == 1 and rx_note[0]["doctor_type"] == "pharmacist" and "Medicines: " in rx_note[0]["content"]
    assert rx_note[0]["content"].startswith("Prescription, 2026-09-01, from Dr. Khan")
    # No per-item notes for diagnoses any more.
    assert not any(m["memory_type"] == "diagnosis" for m in db.list_doctor_memories(pid, ed))
