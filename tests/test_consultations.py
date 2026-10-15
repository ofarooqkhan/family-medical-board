"""Doctor conversations: transcript to summary to pending items. Database tests
run only when TEST_DATABASE_URL points at a throwaway Postgres."""
import datetime as dt
import os

import pytest

import consultations
from providers import FakeProvider, ProviderError, Transcript

TEST_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")

SUMMARY = {
    "title": "Cardiology review with Dr. Lee", "language": "en", "happened_on": "2026-10-10",
    "doctor_name": "Dr. Lee", "doctor_specialty": "Cardiology", "doctor_confidence": 0.9,
    "speakers": [{"label": "A", "role": "doctor", "name": "Dr. Lee", "confidence": 0.9},
                 {"label": "B", "role": "family", "name": None, "confidence": 0.7}],
    "turns": [{"speaker": "A", "text": "The pressure is still high."}, {"speaker": "B", "text": "What should we do?"},
              {"speaker": "A", "text": "Start amlodipine 5 mg once daily and come back in four weeks."}],
    "context": "Blood pressure follow-up.", "summary": "Pressure still high; amlodipine started; review in four weeks.",
    "findings_discussed": ["Blood pressure still high"], "decisions": ["Amlodipine 5 mg started"],
    "actions": [{"text": "Check pressure every morning", "who": "family", "when": None, "kind": "monitoring", "confidence": 0.8},
                {"text": "Come back in four weeks", "who": "family", "when": "in four weeks", "kind": "appointment", "confidence": 0.8}],
    "medication_changes": [
        {"name": "Amlodipine", "generic_name": "amlodipine", "dose": "5 mg", "route": "oral", "frequency": "once daily",
         "change": "start", "reason": "pressure still high", "instructions": None, "confidence": 0.85, "low_confidence_fields": []},
        {"name": "Aspirin", "generic_name": None, "dose": "75 mg", "route": None, "frequency": "once daily", "change": "continue",
         "reason": None, "instructions": None, "confidence": 0.9, "low_confidence_fields": []}],
    "follow_ups": [{"title": "Cardiology review", "when_text": "in four weeks", "date": "2026-11-07", "doctor_name": "Dr. Lee",
                    "specialty": "Cardiology", "confidence": 0.7},
                   {"title": "Echo some time", "when_text": "some time", "date": None, "doctor_name": None, "specialty": None, "confidence": 0.3}],
    "questions_left_open": ["Whether the evening dose can move earlier"], "overall_confidence": 0.8,
}


def _provider(transcript="A: The pressure is still high. B: What should we do? A: Start amlodipine 5 mg once daily."):
    return FakeProvider(responses={"ConsultationSummary": SUMMARY}, transcripts={"*": transcript})


def test_items_from_summary_queues_starts_and_dated_follow_ups_only():
    summary = consultations.ConsultationSummary.model_validate(SUMMARY)
    items = consultations.items_from_summary(summary)
    kinds = [(i["item_type"], i["payload"].get("name") or i["payload"].get("title")) for i in items]
    assert kinds == [("medication", "Amlodipine"), ("appointment", "Cardiology review")]
    med = items[0]["payload"]
    assert med["prescriber"] == "Dr. Lee" and med["start_date"] == "2026-10-10" and med["change"] == "start"
    assert items[1]["payload"]["starts_at"] == "2026-11-07" and items[1]["payload"]["specialty"] == "Cardiology"


def test_summarize_passes_speaker_labels_when_diarized():
    p = _provider()
    t = Transcript(text="x", segments=[{"speaker": "A", "start": 0, "end": 1, "text": "Hello"},
                                       {"speaker": "B", "start": 1, "end": 2, "text": "Hi"}], model="fake")
    summary, call = consultations.summarize_transcript(p, "gpt-4o", t, "Parent One")
    assert summary.doctor_name == "Dr. Lee"
    sent = p.calls[-1]["parts"][0].text
    assert "[A] Hello" in sent and "[B] Hi" in sent and "Parent One" in sent
    assert "never add medical advice" in p.calls[-1]["system"].lower()


@pytest.fixture
def patient(accounts, migrated):
    return migrated.create_patient(accounts["admin"], "Parent One")


@needs_db
def test_ingest_audio_end_to_end(accounts, migrated, patient):
    import documents
    editor = accounts["editor"]
    out = consultations.ingest_audio(patient, editor, "visit.m4a", "audio/mp4", b"RIFF-fake-audio", _provider(),
                                     happened_on=dt.date(2026, 10, 10))
    assert out["error"] is None and out["items"] == 2
    rows = consultations.list_consultations(patient, editor)
    assert len(rows) == 1
    c = rows[0]
    assert c["status"] == "ready" and c["doctor_name"] == "Dr. Lee" and c["title"] == "Cardiology review with Dr. Lee"
    assert c["happened_on"] == dt.date(2026, 10, 10)
    full = consultations.get_consultation(c["id"], patient, editor)
    assert "amlodipine" in full["transcript"].lower()
    assert full["summary"]["actions"][0]["text"] == "Check pressure every morning"
    # The audio is a stored document of its own kind.
    doc = migrated.get_document(c["file_id"], patient, editor, with_data=True)
    assert doc["document_kind"] == "consultation_audio" and bytes(doc["file_data"]) == b"RIFF-fake-audio"
    # Nothing reached the medication list or calendar before confirmation.
    import appointments as appts
    import medications as meds
    assert meds.list_medications(patient, editor) == []
    assert appts.list_appointments(patient, editor) == []
    pending = documents.pending_review(patient, editor)[c["file_id"]]
    med_item = next(i for i in pending if i["item_type"] == "medication")
    appt_item = next(i for i in pending if i["item_type"] == "appointment")
    documents.confirm_item(med_item["id"], patient, editor, med_item["payload"])
    documents.confirm_item(appt_item["id"], patient, editor, appt_item["payload"])
    m = meds.list_medications(patient, editor)
    assert len(m) == 1 and m[0]["name"] == "Amlodipine" and m[0]["source_file_id"] == c["file_id"]
    a = appts.list_appointments(patient, editor)
    assert len(a) == 1 and a[0]["title"] == "Cardiology review" and a[0]["starts_at"] == dt.datetime(2026, 11, 7, 10, 0)
    # Brief carries the conversation and its actions.
    import brief
    b = brief.build_brief(patient, editor, days=3650)
    assert b["conversations"][0]["doctor"] == "Dr. Lee"
    md = brief.brief_markdown(b)
    assert "Doctor conversations" in md and "Check pressure every morning" in md
    # Audit trail.
    actions = {r["action"] for r in migrated.get_audit_log(patient)}
    assert {"consultation.add", "consultation.summarize", "document.upload"} <= actions


@needs_db
def test_ingest_without_provider_or_with_failure(accounts, migrated, patient):
    editor = accounts["editor"]
    out = consultations.ingest_audio(patient, editor, "visit.mp3", "audio/mpeg", b"x", None)
    assert out["error"] == "no model"
    c = consultations.list_consultations(patient, editor)[0]
    assert c["status"] == "error" and "not transcribed" in c["error"]
    # A failing transcription leaves the audio stored with the error, and a retry fixes it.
    bad = FakeProvider(responses={"ConsultationSummary": SUMMARY})
    out2 = consultations.ingest_audio(patient, editor, "visit2.mp3", "audio/mpeg", b"y", bad)
    assert out2["error"] and "no transcript" in out2["error"]
    c2 = consultations.get_consultation(out2["consultation_id"], patient, editor)
    assert c2["status"] == "error"
    consultations.process(c2["id"], patient, editor, _provider())
    assert consultations.get_consultation(c2["id"], patient, editor)["status"] == "ready"


@needs_db
def test_roles_scoping_and_delete(accounts, migrated, patient):
    editor, viewer, admin = accounts["editor"], accounts["viewer"], accounts["admin"]
    with pytest.raises(PermissionError):
        consultations.ingest_audio(patient, viewer, "v.mp3", "audio/mpeg", b"x", _provider())
    out = consultations.ingest_audio(patient, editor, "v.mp3", "audio/mpeg", b"x", _provider())
    other = migrated.create_patient(admin, "Parent Two")
    assert consultations.list_consultations(other, viewer) == []
    assert consultations.get_consultation(out["consultation_id"], other, viewer) is None
    with pytest.raises(PermissionError):
        consultations.update_consultation(out["consultation_id"], other, editor, title="x")
    consultations.update_consultation(out["consultation_id"], patient, editor, title="Renamed", doctor_name="Dr. X")
    assert consultations.get_consultation(out["consultation_id"], patient, viewer)["title"] == "Renamed"
    with pytest.raises(PermissionError):
        consultations.delete_consultation(out["consultation_id"], patient, viewer)
    consultations.delete_consultation(out["consultation_id"], patient, editor)
    assert consultations.list_consultations(patient, viewer) == []
    assert migrated.list_documents(patient, viewer) == []


def test_too_large_upload_is_refused(accounts=None):
    with pytest.raises(ValueError):
        consultations.ingest_audio(1, {"id": "u", "role": "editor"}, "big.wav", "audio/wav",
                                   b"0" * (consultations.MAX_AUDIO_BYTES + 1), None)
